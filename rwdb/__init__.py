"""rw.db provides a simple database ORM for mongodb

Example::

    from rw.www import RequestHandler, get, post
    import motor
    import rw.db
    from tornado import gen


    class User(rw.db.Entity):
        name = rw.db.Field(unicode)
        email = rw.db.Field(unicode)


    class Main(RequestHandler):
        @gen.engine
        @get('/')
        def index(self):
            self['users'] = yield gen.Task(User.query.filter_by(name='2').all)
            self.finish(template='index.html')


To configure a connection rw's :ref:`cfg` is used::

    [mongodb]
    host = 127.0.0.1
    db = my_database
    replica_set = rs1  # optional, only specify if replication is active
    user = me  # optional, specify only if auth is active
    password = pssst  # optional, specify only if auth is active

    [rw.plugins]
    rw.db = True

"""
import numbers
from copy import copy
import re
import warnings
import logging

import bson
import bson.errors
from motor import MotorClient, MotorReplicaSetClient
import pymongo.read_preferences

import rw
import rw.plugin
import rw.routing
from rw import gen, scope

db = None
LOG = logging.getLogger(__name__)


class Cursor(object):
    def __init__(self, query):
        self.col_cls = query.col_cls
        col = query.get_collection()
        self.db_cursor = col.find(query._filters, fields=query._fields, sort=query._sort, limit=query._limit,
                                  skip=query._skip)

    @gen.coroutine
    def to_list(self, length):
        data = yield self.db_cursor.to_list(length)
        raise gen.Return([self.col_cls(**e) for e in data])

    @gen.coroutine
    def to_dict(self, length):
        data = yield self.db_cursor.to_list(length)
        raise gen.Return({e['_id']: self.col_cls(**e) for e in data})

    @gen.coroutine
    def next(self):
        ret = yield self.db_cursor.fetch_next
        if ret:
            raise gen.Return(self.col_cls(**self.db_cursor.next_object()))
        else:
            raise gen.Return(None)

    def skip(self, skip):
        self.db_cursor.skip(skip)
        return self


class Query(object):
    def __init__(self, col_cls, connection, filters=None, sort=None, limit=0, skip=0, fields=None):
        self.col_cls = col_cls
        self._sort = sort
        self._filters = filters if filters else {}
        self._limit = limit
        self._skip = skip
        self._fields = fields
        self._connection = connection

    def __item__(self):
        pass

    def __getitem__(self, slice):
        if isinstance(slice, numbers.Number):
            return self.clone(limit=1, skip=slice)
        elif slice.step is None \
                and isinstance(slice.start, numbers.Number) \
                and isinstance(slice.stop, numbers.Number) \
                and slice.stop > slice.start:
            return self.clone(limit=slice.stop - slice.start, skip=slice.start)
        else:
            raise AttributeError('Slice indecies must be integers, step (= {}) must not be set'
                                 ' and start (= {}) must be higher than stop (= {})'.format(
                slice.step, repr(slice.start), repr(slice.stop)
            ))

    def clone(self, **kwargs):
        params = {
            'filters': self._filters,
            'sort': self._sort,
            'limit': self._limit,
            'skip': self._skip,
            'fields': self._fields
        }
        params.update(kwargs)
        return Query(self.col_cls, self._connection, **params)

    def find(self, *args, **kwargs):
        # we are using *args instead of having named arguments like
        # query=None, fields=None
        # to avoid possibile conflicts with **kwargs
        filters = copy(self._filters)
        filters.update(kwargs)
        if args:
            filters.update(args[0])
            if len(args) > 1:
                self._fields = args[1]
        return self.clone(filters=filters)

    def sort(self, sort):
        return self.clone(sort=sort)

    def limit(self, limit):
        return self.clone(limit=limit)

    def to_list(self, length):
        return Cursor(self).to_list(length)

    def to_dict(self, length):
        return Cursor(self).to_dict(length)

    def cursor(self):
        return Cursor(self)

    @gen.coroutine
    def first(self):
        result = yield self.to_list(1)
        raise gen.Return(result[0] if result else None)

    @gen.coroutine
    def count(self):
        col = self.get_collection()
        ret = yield col.find(self._filters, sort=self._sort, skip=self._skip, limit=self._limit).count()
        raise gen.Return(ret)

    @gen.coroutine
    def find_one(self, *args, **kwargs):
        col = self.get_collection()
        filters = copy(self._filters)
        filters.update(kwargs)
        if args:
            filters.update(args[0])
            if len(args) > 1:
                self._fields = args[1]
        ret = yield col.find_one(filters, sort=self._sort, skip=self._skip, limit=self._limit,
                       fields=self._fields)
        if ret:
            raise gen.Return(self.col_cls(**ret))
        else:
            raise gen.Return(None)

    def get_collection(self):
        databases = rw.scope.get('rwdb:databases')
        return databases[self._connection][self.col_cls._name]


class NoDefaultValue(object):
    pass


class TypeCastException(Exception):
    def __init__(self, name, value, typ, e):
        self.type = typ
        self.name = name
        self.value = value
        self.e = str(e)

    def __str__(self):
        return 'TypeCastException on Attrbute {1}:\n' \
               'Cannot cast value {2} to type {0}\n' \
               'Cast Exception was:\n' \
               '{3}'.format(
            repr(self.type),
            self.name,
            repr(self.value),
            self.e
        )


class Field(property):
    def __init__(self, type_, default=NoDefaultValue, none_allowed=True):
        super(Field, self).__init__(self.get_value, self.set_value)
        self.name = None
        self.none_allowed = none_allowed
        self.type = type_
        self.default = default

    def get_value(self, entity):
        if self.name in entity:
            value = entity[self.name]
        elif self.default is not NoDefaultValue:
            entity[self.name] = value = copy(self.default)
        else:
            raise ValueError('Value not found for "{}"'.format(self.name))
        if not isinstance(value, self.type):
            if not self.none_allowed or value is not None:
                entity[self.name] = self.type(value)
                # try:
                #    entity[self.name] = self.type(value)
                # except TypeCastException as e:
                #     raise e
                # except BaseException as e:
                #    raise TypeCastException(self.name, value, self.type, e)
        return entity[self.name]

    def set_value(self, entity, value):
        entity[self.name] = value

    def __repr__(self):
        return '<Field %s>' % self.name


def Vector(typ):
    """Generate a Vector class that casts all elements to specified type"""

    class VectorClass(list):
        def __init__(self, values=None):
            if values:
                casted_values = [typ(v) for v in values]
                # casted_values = []
                # try:
                #     for i, value in enumerate(values):
                #         casted_values.append(typ(value))
                # except BaseException as e:
                #     raise TypeCastException(str(i), value, typ, e)

                list.__init__(self, casted_values)

        def _check_type(self, value):
            if not isinstance(value, typ):
                raise ValueError('Vector({}) does not accept items of type {}'.format(
                    repr(typ), repr(type(value))
                ))

        def __setitem__(self, key, value):
            self._check_type(value)
            list.__setitem__(self, key, value)

        def append(self, p_object):
            self._check_type(p_object)
            list.append(self, p_object)

        def extend(self, iterable):
            iterable = list(iterable)
            for value in iterable:
                self._check_type(value)
            list.extend(self, iterable)

    return VectorClass


# TODO
class Reference(Field):
    pass


class DocumentMeta(type):
    def __new__(mcs, name, bases, dct):
        ret = type.__new__(mcs, name, bases, dct)

        ret._id_name = '_id'
        for key, value in dct.items():
            if isinstance(value, Field):
                field = getattr(ret, key)
                field.name = key

        if bases != (dict,) and '_name' not in dct:
            ret._name = name.lower()
            # ret.query = Query(ret)

        return ret


class DocumentBase(dict):
    __metaclass__ = DocumentMeta


class SubDocument(DocumentBase):
    pass


class Document(DocumentBase):
    """Base type for mapped classes.

    It is a regular dict, with a little different construction behaviour plus
    one new class method `create` to create a new entry in a collection and
    a new method `delete` to delete an entry.

    Additionally there is a `query` attribute on `Entity` subclasses for


    Example::
         class Fruits(rw.db.Document):
             kind = rw.db.Field(unicode)

         Fuits.find(kind='banana').all()

    Warning: Never use "callback" as key."""

    _id = Field(bson.ObjectId)
    _connection = 'default'

    def __init__(self, *args, **kwargs):
        if len(args) > 2:
            raise AttributeError()
        elif len(args) == 1:
            kwargs.update(args[0])
        cls = self.__class__
        for field in dir(cls):
            cls_obj = getattr(cls, field)
            if isinstance(cls_obj, Field) and cls_obj.default is not NoDefaultValue:
                getattr(self, field)
        self.update(kwargs)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__,
                            ' '.join('%s=%s' % item for item in self.items()))

    def __hash__(self):
        return hash(self._id)

    @gen.coroutine
    def insert(self):
        """Save entry in collection (updates or creates)

        returns Future"""
        ret = yield self.get_collection().insert(self)
        # creating a new entry without an _id MongoDB will
        # generate an id in ObjectId format.
        if not '_id' in self and isinstance(ret, bson.ObjectId):
            self['_id'] = ret
        raise gen.Return(ret)

    @gen.coroutine
    def sync_db(self, upsert=False):
        """update entry in collection (updates or creates)

        returns Future"""
        if upsert and '_id' not in self:
            ret = yield self.get_collection().insert(self)
        else:
            ret = yield self.get_collection().update({'_id': self['_id']}, self, upsert=upsert)
        raise gen.Return(ret)

    @gen.coroutine
    def remove(self):
        ret = yield self.get_collection().remove({'_id': self['_id']})
        raise gen.Return(ret)

    @classmethod
    def find(cls, *args, **kwargs):
        query = Query(cls, cls._connection)
        return query.find(*args, **kwargs)

    @classmethod
    def find_one(cls, *args, **kwargs):
        query = Query(cls, cls._connection)
        return query.find_one(*args, **kwargs)

    @classmethod
    def by_id(cls, _id):
        if not isinstance(_id, cls._id.type):
            _id = cls._id.type(_id)
        return Query(cls, cls._connection).find(_id=_id).find_one()

    @property
    def _motor_collection(self):
        warnings.warn('use get_collection() instead', DeprecationWarning)
        return self.get_collection()

    @classmethod
    def get_collection(cls):
        databases = rw.scope.get('rwdb:databases')
        return databases[cls._connection][cls._name]


class Unicode(Field):
    pass


def extract_model(fileobj, keywords, comment_tags, options):
    """Extract messages from rw models

    :param fileobj: the file-like object the messages should be extracted
                    from
    :param keywords: a list of keywords (i.e. function names) that should
                     be recognized as translation functions
    :param comment_tags: a list of translator tags to search for and
                         include in the results
    :param options: a dictionary of additional options (optional)
    :return: an iterator over ``(lineno, funcname, message, comments)``
             tuples
    :rtype: ``iterator``
    """
    import ast, _ast

    code = ast.parse(fileobj.read()).body
    for statement in code:
        if isinstance(statement, _ast.ClassDef):
            for base in statement.bases:
                cls_name = statement.name
                if base.id in ('Document', 'db.Document', 'rw.db.Document'):
                    for line in statement.body:
                        if isinstance(line, _ast.Assign):
                            for name in line.targets:
                                msg = 'model.{}.{}'.format(cls_name, name.id)
                                yield (name.lineno,
                                       'gettext',
                                       msg.format('1', name.id),
                                       ''
                                )
                                yield (name.lineno,
                                       'gettext',
                                       msg + '-Description',
                                       ''
                                )
                                # yield (base.lineno)


@gen.coroutine
def connect(cfg):
    """connect to mongo database

    :param cfg: Dictionary containing configuration for MongoDB connection
    :type cfg: dict
    """
    if cfg.get('replica_set'):
        LOG.info("connecting to replicaSet %s", cfg['host'])
        client = MotorReplicaSetClient(cfg['host'], replicaSet=cfg['replica_set'])
    else:
        LOG.info("connecting to %s", cfg['host'])
        client = MotorClient(cfg['host'])
    yield client.open()
    if cfg.get('user'):
        yield client[cfg['db']].authenticate(cfg['user'], cfg['password'])
    if cfg.get('read_preference'):
        read_preference = cfg['read_preference'].upper()
        client.read_preference = getattr(pymongo.read_preferences.ReadPreference, read_preference)
    raise gen.Return(client)



NON_HEX = re.compile('[^0-9a-f]')


def routing_converter_object_id(data):
    """converter "ObjectId" for rw.routing

    :param str data:
    """
    length = 24
    try:
        value = bson.ObjectId(data[:length])
    except bson.errors.InvalidId as e:
        raise rw.routing.NoMatchError(str(e))
    return length, value


plugin = rw.plugin.Plugin('rwdb')


@plugin.init
@gen.coroutine
def init(scope, settings):
    cfg = settings.get('rwdb', {})

    connections = []
    keys = []
    for key, client_attr in cfg.items():
        # populate defaults
        for default_key in ['host', 'db', 'user', 'password']:
            if default_key in cfg and not default_key in client_attr:
                client_attr[default_key] = cfg['default'][default_key]
        keys.append(key)
        connections.append(connect(client_attr))

    connections = yield connections
    clients = dict(zip(keys, connections))
    databases = {
        key: clients[key][cfg[key]['db']] for key in keys
    }
    scope['rwdb:clients'] = clients
    scope['rwdb:databases'] = databases
    scope.setdefault('rw.routing:converters', {}).update({
        'ObjectId': routing_converter_object_id
    })


# TODO @plugin.shutdown
# @gen.coroutine
# def shutdown():
#     for client in CLIENTS.itervalues():
#         client.disconnect()
