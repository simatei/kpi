# coding: utf-8
import re

from bson import ObjectId
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext as _

from kpi.constants import NESTED_MONGO_RESERVED_ATTRIBUTES
from kpi.utils.strings import base64_encodestring


class MongoHelper:
    """
    Mongo's helper.

    Mix of KoBoCAT's onadata.apps.api.mongo_helper.MongoHelper
    and KoBoCAT's ParseInstance class to query mongo.
    """

    KEY_WHITELIST = ['$or', '$and', '$exists', '$in', '$gt', '$gte',
                     '$lt', '$lte', '$regex', '$options', '$all']

    ENCODING_SUBSTITUTIONS = [
        (re.compile(r'^\$'), base64_encodestring('$').strip()),
        (re.compile(r'\.'), base64_encodestring('.').strip()),
    ]

    DECODING_SUBSTITUTIONS = [
        (re.compile(r'^' + base64_encodestring('$').strip()), '$'),
        (re.compile(base64_encodestring('.').strip()), '.'),
    ]

    # Match KoBoCAT's variables of ParsedInstance class
    USERFORM_ID = '_userform_id'
    DEFAULT_BATCHSIZE = 1000

    @classmethod
    def decode(cls, key):
        """
        Replace base64-encoded characters not allowed in Mongo keys with their
        original representations

        :param key: string
        :return: string
        """
        for pattern, repl in cls.DECODING_SUBSTITUTIONS:
            key = re.sub(pattern, repl, key)
        return key

    @classmethod
    def encode(cls, key):
        """
        Replace characters not allowed in Mongo keys with their base64-encoded
        representations

        :param key: string
        :return: string
        """
        for pattern, repl in cls.ENCODING_SUBSTITUTIONS:
            key = re.sub(pattern, repl, key)
        return key

    @classmethod
    def get_count(
            cls, mongo_userform_id, hide_deleted=True, query=None, instance_ids=None,
            permission_filters=None):

        _, total_count = cls._get_cursor_and_count(
            mongo_userform_id,
            hide_deleted=hide_deleted,
            fields={'_id': 1},
            query=query,
            instance_ids=instance_ids,
            permission_filters=permission_filters)

        return total_count

    @classmethod
    def get_instances(
            cls, mongo_userform_id, hide_deleted=True, start=None, limit=None,
            sort=None, fields=None, query=None, instance_ids=None,
            permission_filters=None
    ):
        cursor, total_count = cls._get_cursor_and_count(
            mongo_userform_id,
            hide_deleted=hide_deleted,
            fields=fields,
            query=query,
            instance_ids=instance_ids,
            permission_filters=permission_filters)

        cursor.skip(start)
        if limit is not None:
            cursor.limit(limit)

        if len(sort) == 1:
            sort = MongoHelper.to_safe_dict(sort, reading=True)
            sort_key = list(sort.keys())[0]
            sort_dir = int(sort[sort_key])  # -1 for desc, 1 for asc
            cursor.sort(sort_key, sort_dir)

        # set batch size
        cursor.batch_size = cls.DEFAULT_BATCHSIZE

        return cursor, total_count

    @classmethod
    def is_attribute_invalid(cls, key):
        """
        Checks if an attribute can't be passed to Mongo as is.
        :param key:
        :return:
        """
        return key not in cls.KEY_WHITELIST and\
               (key.startswith('$') or key.count('.') > 0)

    @classmethod
    def to_readable_dict(cls, d):
        """
        Updates encoded attributes of a dict with human-readable attributes.
        For example:
        { "myLg==attribute": True } => { "my.attribute": True }

        :param d: dict
        :return: dict
        """

        for key, value in list(d.items()):
            if type(value) == list:
                value = [cls.to_readable_dict(e)
                         if type(e) == dict else e for e in value]
            elif type(value) == dict:
                value = cls.to_readable_dict(value)

            if cls._is_attribute_encoded(key):
                del d[key]
                d[cls.decode(key)] = value

        return d

    @classmethod
    def to_safe_dict(cls, d, reading=False):
        """
        Updates invalid attributes of a dict by encoding disallowed characters
        and, when `reading=False`, expanding dotted keys into nested dicts for
        `NESTED_MONGO_RESERVED_ATTRIBUTES`

        :param d: dict
        :param reading: boolean.
        :return: dict

        Example:

            >>> d = {
                    '_validation_status.other.nested': 'lorem',
                    '_validation_status.uid': 'approved',
                    'my.string.with.dots': 'yes'
                }
            >>> MongoHelper.to_safe_dict(d)
                {
                    'myLg==stringLg==withLg==dots': 'yes',
                    '_validation_status': {
                        'other': {
                            'nested': 'lorem'
                        },
                        'uid': 'approved'
                    }
                }
            >>> MongoHelper.to_safe_dict(d, reading=True)
                {
                    'myLg==stringLg==withLg==dots': 'yes',
                    '_validation_status.other.nested': 'lorem',
                    '_validation_status.uid': 'approved'
                }
        """
        for key, value in list(d.items()):
            if type(value) == list:
                value = [cls.to_safe_dict(e, reading=reading)
                         if type(e) == dict else e for e in value]
            elif type(value) == dict:
                value = cls.to_safe_dict(value, reading=reading)
            elif key == '_id':
                try:
                    d[key] = int(value)
                except ValueError:
                    # if it is not an int don't convert it
                    pass

            if cls._is_nested_reserved_attribute(key):
                # If we want to write into Mongo, we need to transform the dot delimited string into a dict
                # Otherwise, for reading, Mongo query engine reads dot delimited string as a nested object.
                # Drawback, if a user uses a reserved property with dots, it will be converted as well.
                if not reading and key.count(".") > 0:
                    tree = {}
                    t = tree
                    parts = key.split(".")
                    last_index = len(parts) - 1
                    for index, part in enumerate(parts):
                        v = value if index == last_index else {}
                        t = t.setdefault(part, v)
                    del d[key]
                    first_part = parts[0]
                    if first_part not in d:
                        d[first_part] = {}

                    # We update the main dict with new dict.
                    # We use dict_for_mongo again on the dict to ensure, no invalid characters are children
                    # elements
                    d[first_part].update(cls.to_safe_dict(tree[first_part]))

            elif cls.is_attribute_invalid(key):
                del d[key]
                d[cls.encode(key)] = value

        return d

    @classmethod
    def encode(cls, key):
        """
        Replace characters not allowed in Mongo keys with their base64-encoded
        representations

        :param key: string
        :return: string
        """
        for pattern, repl in cls.ENCODING_SUBSTITUTIONS:
            key = re.sub(pattern, repl, key)
        return key

    @classmethod
    def decode(cls, key):
        """
        Replace base64-encoded characters not allowed in Mongo keys with their
        original representations

        :param key: string
        :return: string
        """
        for pattern, repl in cls.DECODING_SUBSTITUTIONS:
            key = re.sub(pattern, repl, key)
        return key

    @classmethod
    def is_attribute_invalid(cls, key):
        """
        Checks if an attribute can't be passed to Mongo as is.
        :param key:
        :return:
        """
        return key not in \
               cls.KEY_WHITELIST and (key.startswith('$') or key.count('.') > 0)

    @classmethod
    def _get_cursor_and_count(cls, mongo_userform_id, hide_deleted=True,
                              fields=None, query=None, instance_ids=None,
                              permission_filters=None):

        if len(instance_ids) > 0:
            query.update({
                '_id': {'$in': instance_ids}
            })

        query.update({cls.USERFORM_ID: mongo_userform_id})

        # Narrow down query
        if permission_filters is not None:
            permission_filters_query = {'$or': []}
            for permission_filter in permission_filters:
                permission_filters_query['$or'].append(permission_filter)

            query = {'$and': [query, permission_filters_query]}

        if hide_deleted:
            # display only active elements
            deleted_at_query = {
                '$or': [{'_deleted_at': {'$exists': False}},
                        {'_deleted_at': None}]}
            # join existing query with deleted_at_query on an $and
            query = {'$and': [query, deleted_at_query]}

        query = cls.to_safe_dict(query, reading=True)

        if len(fields) > 0:
            # Retrieve only specified fields from Mongo. Remove
            # `cls.USERFORM_ID` from those fields in case users try to add it.
            if cls.USERFORM_ID in fields:
                fields.remove(cls.USERFORM_ID)
            fields_to_select = dict(
                [(cls.encode(field), 1) for field in fields])
        else:
            # Retrieve all fields except `cls.USERFORM_ID`
            fields_to_select = {cls.USERFORM_ID: 0}

        cursor = settings.MONGO_DB.instances.find(
            query,
            fields_to_select,
            max_time_ms=settings.MONGO_DB_MAX_TIME_MS
        )
        return cursor, cursor.count()

    @classmethod
    def _is_attribute_encoded(cls, key):
        """
        Checks if an attribute has been encoded when saved in Mongo.

        :param key: string
        :return: string
        """
        return key not in cls.KEY_WHITELIST and (key.startswith('JA==') or
                                                 key.count('Lg==') > 0)

    @staticmethod
    def _is_nested_reserved_attribute(key):
        """
        Checks if key starts with one of variables values declared in NESTED_MONGO_RESERVED_ATTRIBUTES

        :param key: string
        :return: boolean
        """
        for reserved_attribute in NESTED_MONGO_RESERVED_ATTRIBUTES:
            if key.startswith("{}.".format(reserved_attribute)):
                return True
        return False
