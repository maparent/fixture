
"""Components for loading and unloading data using `SQLAlchemy`_.

.. _SQLAlchemy: http://www.sqlalchemy.org/

"""

import sys
from fixture.loadable import DBLoadableFixture
from fixture.exc import UninitializedError
import logging

log = logging.getLogger('fixture.loadable.sqlalchemy_loadable')

from sqlalchemy.orm import sessionmaker, scoped_session
Session = scoped_session(sessionmaker(autoflush=False, transactional=True), scopefunc=lambda:__name__)

def negotiated_medium(obj, dataset):
    if is_table(obj):
        return TableMedium(obj, dataset)
    elif is_assigned_mapper(obj):
        return MappedClassMedium(obj, dataset)
    elif is_mapped_class(obj):
        return MappedClassMedium(obj, dataset)
    else:
        raise NotImplementedError("object %s is not supported by %s" % (
                                                    obj, SQLAlchemyFixture))

class SQLAlchemyFixture(DBLoadableFixture):
    """
    A fixture that knows how to load DataSet objects into `SQLAlchemy`_ objects.
    
    Keyword Arguments:
    
    style
        A Style object to translate names with
    
    scoped_session
        An class-like Session created by sqlalchemy.orm.scoped_session() .  
        Only declare a custom Session if you have to.  The preferred way 
        is to let fixture use its own Session in a private scope.
    
    engine
        A specific connectable/engine object to use when one is not bound.  
        engine.connect() will be called.
    
    session
        A session from sqlalchemy.create_session().  This will override the 
        ScopedSession and SessionContext approaches.  Only declare a session if you have to.  
        The preferred way is to let fixture use its own session in a private scope.
    
    connection
        A specific connectable/engine object (must be connected) to use 
        when one is not bound.
    
    dataclass
        SuperSet to represent loaded data with
    
    env
        A dict or module that contains either mapped classes or Table objects,
        or both.  This will be search when style translates DataSet names into
        storage media.
    
    medium
        A custom StorageMediumAdapter to instantiate when storing a DataSet.
        By default, a medium adapter will be negotiated based on the type of 
        sqlalchemy object so you should only set this if you know what you 
        doing.
    
    """
    Medium = staticmethod(negotiated_medium)
    
    def __init__(self, engine=None, connection=None, session=None, scoped_session=None, **kw):
        from sqlalchemy.orm import sessionmaker # ensure import error
        DBLoadableFixture.__init__(self, **kw)
        self.engine = engine
        self.connection = connection
        self.session = session
        if scoped_session is None:
            scoped_session = Session
        self.Session = scoped_session
    
    def begin(self, unloading=False):
        """begin loading data
        
        - creates and stores a connection with engine.connect() if an engine was passed
          
          - binds the connection or engine to fixture's internal session
          
        - uses an unbound internal session if no engine or connection was passed in
        """
        if not unloading:
            # ...then we are loading, so let's *lazily* 
            # clean up after a previous setup/teardown
            Session.remove()
        if self.connection is None and self.engine is None:
            if self.session:
                self.engine = self.session.bind # might be None
        
        if self.engine is not None and self.connection is None:
            self.connection = self.engine.connect()
        
        if self.session is None:
            if self.connection:
                self.Session.configure(bind=self.connection)
            else:
                self.Session.configure(bind=None)
            self.session = self.Session()
            
        DBLoadableFixture.begin(self, unloading=unloading)
    
    def commit(self):
        """commit the load transaction and flush the session
        """
        if self.connection:
            # note that when not using a connection, calling session.commit() 
            # as the inheirted code does will automatically flush the session
            self.session.flush()
        
        log.debug("transaction.commit() <- %s", self.transaction)
        DBLoadableFixture.commit(self)
    
    def create_transaction(self):
        """create a session or connection transaction
        
        - if a custom connection was used, calls connection.begin
        - otherwise calls session.begin()
        
        """
        if self.connection is not None:
            log.debug("connection.begin()")
            transaction = self.connection.begin()
        else:
            transaction = self.session.begin()
        log.debug("create_transaction() <- %s", transaction)
        return transaction
    
    def dispose(self):
        """dispose of this fixture instance entirely
        
        Closes all connection, session, and transaction objects and calls engine.dispose()
        
        After calling fixture.dispose() you cannot use the fixture instance.  
        Instead you have to create a new instance like::
        
            fixture = SQLAlchemyFixture(...)
        
        """
        from fixture.dataset import dataset_registry
        dataset_registry.clear()
        if self.connection:
            self.connection.close()
        if self.session:
            self.session.close()
        if self.transaction:
            self.transaction.close()
        if self.engine:
            self.engine.dispose()
    
    def rollback(self):
        """rollback load transaction"""
        DBLoadableFixture.rollback(self)

## this was used in an if branch of clear() ... but I think this is no longer necessary with scoped sessions
## does it need to exist for 0.4 ?  not sure
# def object_was_deleted(session, obj):
#     # hopefully there is a more future proof way to do this...
#     from sqlalchemy.orm.mapper import object_mapper
#     for c in [obj] + list(object_mapper(obj).cascade_iterator(
#                                                     'delete', obj)):
#         if c in session.deleted:
#             return True
#         elif not session.uow._is_valid(c):
#             # it must have been deleted elsewhere.  is there any other 
#             # reason for this scenario?
#             return True
#     return False

class MappedClassMedium(DBLoadableFixture.StorageMediumAdapter):
    """
    Adapter for `SQLAlchemy`_ mapped classes.
    
    For example, in ``mapper(TheClass, the_table)`` ``TheClass`` is a mapped class.
    If using `Elixir`_ then any class descending from ``elixir.Entity`` is treated like a mapped class.
    
    .. _Elixir: http://elixir.ematia.de/
    
    """
    def __init__(self, *a,**kw):
        DBLoadableFixture.StorageMediumAdapter.__init__(self, *a,**kw)
        
    def clear(self, obj):
        """delete this object from the session"""
        self.session.delete(obj)
    
    def visit_loader(self, loader):
        """visits the :class:`SQLAlchemyFixture` loader and stores a reference to its session"""
        self.session = loader.session
        
    def save(self, row, column_vals):
        """save a new object to the session if it doesn't already exist in the session."""
        obj = self.medium()
        for c, val in column_vals:
            setattr(obj, c, val)
        if obj not in self.session.new:
            self.session.save(obj)
        return obj


class LoadedTableRow(object):
    def __init__(self, table, inserted_key, conn):
        self.table = table
        self.conn = conn
        self.inserted_key = [k for k in inserted_key]
        self.row = None
    
    def __getattr__(self, col):
        if not self.row:
            if len(self.inserted_key) > 1:
                raise NotImplementedError(
                    "%s does not support making a select statement with a "
                    "composite key, %s.  This is probably fixable" % (
                                        self.__class__.__name__, 
                                        self.table.primary_key))
            
            first_pk = [k for k in self.table.primary_key][0]
            id = getattr(self.table.c, first_pk.key)
            stmt = self.table.select(id==self.inserted_key[0])
            if self.conn:
                c = self.conn.execute(stmt)
            else:
                c = stmt.execute()
            self.row = c.fetchone()
        return getattr(self.row, col)
             
class TableMedium(DBLoadableFixture.StorageMediumAdapter):
    """
    Adapter for `SQLAlchemy Table objects`_
    
    If no connection or engine is configured in the :class:`SQLAlchemyFixture` 
    then statements will be executed directly on the Table object itself which adheres 
    to `implicit connection rules`_.  Otherwise, 
    the respective connection or engine will be used to execute statements.
    
    .. _SQLAlchemy Table objects: http://www.sqlalchemy.org/docs/04/ormtutorial.html#datamapping_tables
    .. _implicit connection rules: http://www.sqlalchemy.org/docs/04/dbengine.html#dbengine_implicit
    
    """
            
    def __init__(self, *a,**kw):
        DBLoadableFixture.StorageMediumAdapter.__init__(self, *a,**kw)
        self.conn = None
        
    def clear(self, obj):
        """constructs a delete statement per each primary key and 
        executes it either explicitly or implicitly
        """
        i=0
        for k in obj.table.primary_key:
            id = getattr(obj.table.c, k.key)
            stmt = obj.table.delete(id==obj.inserted_key[i])
            if self.conn:
                c = self.conn.execute(stmt)
            else:
                c = stmt.execute()
            i+=1
    
    def visit_loader(self, loader):
        """visits the :class:`SQLAlchemyFixture` loader and stores a reference to its connection if there is one."""
        if loader.connection:
            self.conn = loader.connection
        else:
            self.conn = None
        
    def save(self, row, column_vals):
        """constructs an insert statement with the given values and 
        executes it either explicitly or implicitly
        """
        from sqlalchemy.schema import Table
        if not isinstance(self.medium, Table):
            raise ValueError(
                "medium %s must be a Table instance" % self.medium)
                
        stmt = self.medium.insert()
        params = dict(list(column_vals))
        if self.conn:
            c = self.conn.execute(stmt, params)
        else:
            c = stmt.execute(params)
        primary_key = c.last_inserted_ids()
        if primary_key is None:
            raise NotImplementedError(
                    "what can we do with a None primary key?")
        table_keys = [k for k in self.medium.primary_key]
        inserted_keys = [k for k in primary_key]
        if len(inserted_keys) != len(table_keys):
            raise ValueError(
                "expected primary_key %s, got %s (using table %s)" % (
                                table_keys, inserted_keys, self.medium))
        
        return LoadedTableRow(self.medium, primary_key, self.conn)

def is_assigned_mapper(obj):
    from sqlalchemy.orm.mapper import Mapper
    if hasattr(obj, 'is_assigned'):
        # 0.4 :
        is_assigned = obj.is_assigned
    else:
        def is_assigned(obj):
            # 0.3 :
           return hasattr(obj, 'mapper') and isinstance(obj.mapper, Mapper)
    return is_assigned(obj)

def is_mapped_class(obj):
    from sqlalchemy import util
    return hasattr(obj, 'c') and isinstance(obj.c, util.OrderedProperties)

def is_table(obj):
    from sqlalchemy.schema import Table
    return isinstance(obj, Table)
