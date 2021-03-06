'''
Provides a subclass of peewee Module ``VersionModule`` that automatically
adds a *_versions class and connects it to the proper signals
'''
import datetime

from six import with_metaclass  # py2 compat
from peewee import (BaseModel, Model, DateTimeField, ForeignKeyField, IntegerField, BooleanField,
                    PrimaryKeyField, RelationDescriptor)


class MetaModel(BaseModel):
    '''
    A MetaClass that automatically creates a nested subclass to track changes

    The nested subclass is referred to as ``VersionModel``
    '''
    # Attribute of the parent class where the ``VersionModel`` can be accessed: Parent._VersionModel
    _version_model_attr_name = '_VersionModel'
    _version_model_name_suffix = 'Version'  # Example, People -> PeopleVersion
    _version_model_related_name = '_versions'  # Example People._versions.get()
    _RECURSION_BREAK_TEST = object()

    def __new__(self, name, bases, attrs):
        # Because the nested VersionModel shares this metaclass, we need to
        # test for it and act like :class:`peewee.BaseModel`
        if (attrs.pop('_RECURSION_BREAK_TEST', None) or
                name == 'VersionedModel'):  # We don't want versions for the mixin
            VersionModel = BaseModel.__new__(self, name, bases, attrs)
            # Because ``VersionModel`` inherits from the initial class
            # we need to mask the reference to itself that is inherited to avoid
            # infinite recursion and for detection
            setattr(VersionModel, self._version_model_attr_name, None)
            return VersionModel

        # Instantiate the fields we want to add
        # These fields will be added to the nested ``VersionModel``
        _version_fields = {'_valid_from': DateTimeField(default=datetime.datetime.now, index=True),
                           '_valid_until': DateTimeField(null=True, default=None,),
                           '_deleted': BooleanField(default=False),
                           '_original_record': None,  # ForeignKeyField. Added later.
                           '_original_record_id': None,  # added later by peewee
                           '_version_id': IntegerField(default=1),
                           '_id': PrimaryKeyField(primary_key=True)}  # Make an explicit primary key

        # Create the class, create the nested ``VersionModel``, link them together.
        for field in attrs.keys():
            if field in _version_fields:
                raise ValueError('You can not declare the attribute {}. '
                                 'It is automatically created by VersionedModel'.format(field))

        # Create the top level ``VersionedModel`` class
        new_class = super(MetaModel, self).__new__(self, name, bases, attrs)

        # Mung up the attributes for our ``VersionModel``
        version_model_attrs = _version_fields.copy()
        version_model_attrs['__qualname__'] = name + self._version_model_name_suffix

        # Add ForeignKeyField linking to the original record
        version_model_attrs['_original_record'] = ForeignKeyField(
            new_class, related_name=self._version_model_related_name, 
            null=True, on_delete="SET NULL"
        )

        # Mask all ``peewee.RelationDescriptor`` fields to avoid related name conflicts
        for field, value in vars(new_class).items():
            if isinstance(value, RelationDescriptor):
                version_model_attrs[field] = None

        # needed to avoid infinite recursion
        version_model_attrs['_RECURSION_BREAK_TEST'] = self._RECURSION_BREAK_TEST

        # Create the nested ``VersionedModel`` class that inherits from the top level new_class
        VersionModel = type(name + self._version_model_name_suffix,  # Name
                            (new_class,),  # bases
                            version_model_attrs)  # attributes
        # Modify the nested ``VersionedModel``
        setattr(VersionModel, '_version_fields', _version_fields)

        # Modify the newly created class before returning
        setattr(new_class, self._version_model_attr_name, VersionModel)
        setattr(new_class, '_version_model_attr_name', self._version_model_attr_name)

        return new_class


# Needed to allow subclassing with differing metaclasses. In this case, BaseModel and Type
class VersionedModel(with_metaclass(MetaModel, Model)):

    @classmethod
    def _is_version_model(cls):
        '''
        If this class is a nested ``VersionModel`` class created by :class:`MetaModel`
        this will return ``True``

        :return: bool
        '''
        return cls._get_version_model() is None

    @classmethod
    def _get_version_model(cls):
        '''
        :return: nested ``VersionModel``
        '''
        version_model = getattr(cls, cls._version_model_attr_name, None)
        return version_model

    def save(self, *args, **kwargs):
        # Default behaviour if this is a ``VersionModel``
        # Only update ``VersionModel if something has changed
        if (self._is_version_model() or
                not self.is_dirty()):
            return super(VersionedModel, self).save(*args, **kwargs)

        # wrap everything in a transaction: all or none
        with self._meta.database.atomic():
            # Save the parent
            super(VersionedModel, self).save(*args, **kwargs)

            # Finalize the previous version
            self._finalize_current_version()

            # Save the new version
            self._create_new_version()

    def delete_instance(self, *args, **kwargs):
        if not self._is_version_model():
            # wrap everything in a transaction: all or none
            with self._meta.database.atomic():
    
                # finalize the previous version
                self._finalize_current_version()
    
                # create a new version initialized to current values
                new_version = self._create_new_version(save=False)
                new_version._deleted = True
                new_version.save()
            
        # default behaviour
        return super(VersionedModel, self).delete_instance(*args, **kwargs)

    @classmethod
    def create_table(cls, *args, **kwargs):
        # create the normal table schema
        super(VersionedModel, cls).create_table(*args, **kwargs)

        if not cls._is_version_model():
            # Create the tables for the nested version model, skip if it is the nested version model
            version_model = getattr(cls, cls._version_model_attr_name, None)
            version_model.create_table(*args, **kwargs)

    @classmethod
    def drop_table(cls, *args, **kwargs):
        # drop the nested ``VersionModel`` table first
        if not cls._is_version_model():
            version_model = getattr(cls, cls._version_model_attr_name, None)
            version_model.drop_table(*args, **kwargs)
            
        # default behaviour
        super(VersionedModel, cls).drop_table(*args, **kwargs)
        

    @property
    def version_id(self):
        '''
        :return: the version_id of the current version or ``None``

        '''
        if not self._is_version_model():
            current_version = self._get_current_version()
            return current_version.version_id
        else:
            return self._version_id

    def revert(self, version):
        '''
        Changes all attributes to match what was saved in ``version``
        This, in itself creates a new version.

        :param version:
          * type ``VersionModel`` match the passed in ``version``
          * type int
            * positive: ``version`` matches ``VersionModel._version_id``
            * negative: negative indexing on ``version``:
              -1 matches the previous version,
              -2 matches two versions ago etc.
        '''
        if self._is_version_model():
            raise RuntimeError('method revert can not be called on a VersionModel')

        VersionModel = self._get_version_model()
        if isinstance(version, VersionModel):
            version_model = version
        elif version >= 0:
            version_model = self._versions.filter(VersionModel._version_id == version).get()
        else:  # version < 0
            version_model = (self._versions
                             .order_by(VersionModel._version_id.desc())
                             .offset(-version)
                             .limit(1))[0]

        fields_to_copy = self._get_fields_to_copy()
        for field in fields_to_copy:
            setattr(self, field, getattr(version_model, field))

        self.save()

    @classmethod
    def _get_fields_to_copy(cls):
        VersionModel = cls._get_version_model()
        version_model_fields_dict = VersionModel._meta.fields
        fields = []
        for key in version_model_fields_dict.keys():
            if key not in VersionModel._version_fields:
                fields.append(key)
        return fields

    def _create_new_version(self, save=True):
        '''
        Creates a new row of ``VersionModel`` and initializes
        it's fields to match the parent.

        :param bool save: should the new_version be saved before returning?
        :return: the newly created instance of ``VersionModel``
        '''

        VersionModel = self._get_version_model()
        # Increment the version id to be one higher than the previous
        try:
            old_version = (self._versions
                           .select()
                           .order_by(VersionModel._version_id.desc())
                           .limit(1))[0]
            new_version_id = old_version.version_id + 1
        except IndexError:
            new_version_id = 1

        new_version = VersionModel()

        fields_to_copy = self._get_fields_to_copy()
        for field in fields_to_copy:
            setattr(new_version, field, getattr(self, field))
        new_version._original_record = self
        new_version._version_id = new_version_id
        if save is True:
            new_version.save()
        return new_version

    def _get_current_version(self):
        '''
        :return: current version or ``None`` if not found
        '''
        VersionModel = self._get_version_model()
        try:
            current_version = (self._versions.select()
                               .where(VersionModel._valid_until.is_null())
                               )  # null record
            assert(len(current_version) == 1)
            return current_version[0]
        except VersionModel.DoesNotExist:
            return None
        except AssertionError:
            if len(current_version) == 0:
                return None
            else:
                raise RuntimeError('Problem with the database. '
                                   'More than one current version was found for {}'
                                   .format(self.__class__))

    def _finalize_current_version(self):
        current_version = self._get_current_version()
        if current_version is not None:
            current_version._valid_until = datetime.datetime.utcnow()
            current_version.save()
