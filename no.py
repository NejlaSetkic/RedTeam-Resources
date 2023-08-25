BACK TO PROJECTS
PRINTEGRATIONONPREM-NEJLA-GH-APP-PROJECT1
Dashboard
Scope

Goals

Code

Hotspots
Hotspot Code Health
Change Coupling
Performed Refactorings
Architecture

Team Dynamics

System

Delivery Performance

Simulations

Configuration
GLOBAL
Configure

Learn

Logout
Hotspot Code Health
Track the health of key files in your codebase: hotspots and the files with goals. 

Search file...

Hotspot
2022
Jul 2023
20/08/2023
Code Health Trend
Details
newly.py
295 LoC | 1 commits
Plan a goal
X-ray
Code Review
Healthy
Attention
Risky
7.78
Bumpy Road
Many Conditionals
Complex Method
Complex Conditional
Read More
View Code
1
Codescene Enterprise (6.2.10-116-g47d17) © 2016-2023

RedTeam-Resources/newly.py
import logging
import operator
from datetime import datetime

from django.db.backends.ddl_references import (
    Columns,
    Expressions,
    ForeignKeyName,
    IndexName,
    Statement,
    Table,
)
from django.db.backends.utils import names_digest, split_identifier
from django.db.models import Deferrable, Index
from django.db.models.sql import Query
from django.db.transaction import TransactionManagementError, atomic
from django.utils import timezone

logger = logging.getLogger("django.db.backends.schema")


def _is_relevant_relation(relation, altered_field):
    """
    When altering the given field, must constraints on its model from the given
    relation be temporarily dropped?
    """
    field = relation.field
    if field.many_to_many:
        # M2M reverse field
        return False
    if altered_field.primary_key and field.to_fields == [None]:
        # Foreign key constraint on the primary key, which is being altered.
        return True
    # Is the constraint targeting the field being altered?
    return altered_field.name in field.to_fields


def _all_related_fields(model):
    # Related fields must be returned in a deterministic order.
    return sorted(
        model._meta._get_fields(
            forward=False,
            reverse=True,
            include_hidden=True,
            include_parents=False,
        ),
        key=operator.attrgetter("name"),
    )


def _related_non_m2m_objects(old_field, new_field):
    # Filter out m2m objects from reverse relations.
    # Return (old_relation, new_relation) tuples.
    related_fields = zip(
        (
            obj
            for obj in _all_related_fields(old_field.model)
            if _is_relevant_relation(obj, old_field)
        ),
        (
            obj
            for obj in _all_related_fields(new_field.model)
            if _is_relevant_relation(obj, new_field)
        ),
    )
    for old_rel, new_rel in related_fields:
        yield old_rel, new_rel
        yield from _related_non_m2m_objects(
            old_rel.remote_field,
            new_rel.remote_field,
        )


class BaseDatabaseSchemaEditor:
    """
    This class and its subclasses are responsible for emitting schema-changing
    statements to the databases - model creation/removal/alteration, field
    renaming, index fiddling, and so on.
    """

    # Overrideable SQL templates
    sql_create_table = "CREATE TABLE %(table)s (%(definition)s)"
    sql_rename_table = "ALTER TABLE %(old_table)s RENAME TO %(new_table)s"
    sql_retablespace_table = "ALTER TABLE %(table)s SET TABLESPACE %(new_tablespace)s"
    sql_delete_table = "DROP TABLE %(table)s CASCADE"

    sql_create_column = "ALTER TABLE %(table)s ADD COLUMN %(column)s %(definition)s"
    sql_alter_column = "ALTER TABLE %(table)s %(changes)s"
    sql_alter_column_type = "ALTER COLUMN %(column)s TYPE %(type)s"
    sql_alter_column_null = "ALTER COLUMN %(column)s DROP NOT NULL"
    sql_alter_column_not_null = "ALTER COLUMN %(column)s SET NOT NULL"
    sql_alter_column_default = "ALTER COLUMN %(column)s SET DEFAULT %(default)s"
    sql_alter_column_no_default = "ALTER COLUMN %(column)s DROP DEFAULT"
    sql_alter_column_no_default_null = sql_alter_column_no_default
    sql_alter_column_collate = "ALTER COLUMN %(column)s TYPE %(type)s%(collation)s"
    sql_delete_column = "ALTER TABLE %(table)s DROP COLUMN %(column)s CASCADE"
    sql_rename_column = (
        "ALTER TABLE %(table)s RENAME COLUMN %(old_column)s TO %(new_column)s"
    )
    sql_update_with_default = (
        "UPDATE %(table)s SET %(column)s = %(default)s WHERE %(column)s IS NULL"
    )

    sql_unique_constraint = "UNIQUE (%(columns)s)%(deferrable)s"
    sql_check_constraint = "CHECK (%(check)s)"
    sql_delete_constraint = "ALTER TABLE %(table)s DROP CONSTRAINT %(name)s"
    sql_constraint = "CONSTRAINT %(name)s %(constraint)s"

    sql_create_check = "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s CHECK (%(check)s)"
    sql_delete_check = sql_delete_constraint

    sql_create_unique = (
        "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s "
        "UNIQUE (%(columns)s)%(deferrable)s"
    )
    sql_delete_unique = sql_delete_constraint

    sql_create_fk = (
        "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s FOREIGN KEY (%(column)s) "
        "REFERENCES %(to_table)s (%(to_column)s)%(deferrable)s"
    )
    sql_create_inline_fk = None
    sql_create_column_inline_fk = None
    sql_delete_fk = sql_delete_constraint

    sql_create_index = (
        "CREATE INDEX %(name)s ON %(table)s "
        "(%(columns)s)%(include)s%(extra)s%(condition)s"
    )
    sql_create_unique_index = (
        "CREATE UNIQUE INDEX %(name)s ON %(table)s "
        "(%(columns)s)%(include)s%(condition)s"
    )
    sql_rename_index = "ALTER INDEX %(old_name)s RENAME TO %(new_name)s"
    sql_delete_index = "DROP INDEX %(name)s"

    sql_create_pk = (
        "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s PRIMARY KEY (%(columns)s)"
    )
    sql_delete_pk = sql_delete_constraint

    sql_delete_procedure = "DROP PROCEDURE %(procedure)s"

    def __init__(self, connection, collect_sql=False, atomic=True):
        self.connection = connection
        self.collect_sql = collect_sql
        if self.collect_sql:
            self.collected_sql = []
        self.atomic_migration = self.connection.features.can_rollback_ddl and atomic

    # State-managing methods

    def __enter__(self):
        self.deferred_sql = []
        if self.atomic_migration:
            self.atomic = atomic(self.connection.alias)
            self.atomic.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            for sql in self.deferred_sql:
                self.execute(sql)
        if self.atomic_migration:
            self.atomic.__exit__(exc_type, exc_value, traceback)

    # Core utility functions

    def execute(self, sql, params=()):
        """Execute the given SQL statement, with optional parameters."""
        # Don't perform the transactional DDL check if SQL is being collected
        # as it's not going to be executed anyway.
        if (
            not self.collect_sql
            and self.connection.in_atomic_block
            and not self.connection.features.can_rollback_ddl
        ):
            raise TransactionManagementError(
                "Executing DDL statements while in a transaction on databases "
                "that can't perform a rollback is prohibited."
            )
        # Account for non-string statement objects.
        sql = str(sql)
        # Log the command we're running, then run it
        logger.debug(
            "%s; (params %r)", sql, params, extra={"params": params, "sql": sql}
        )
        if self.collect_sql:
            ending = "" if sql.rstrip().endswith(";") else ";"
            if params is not None:
                self.collected_sql.append(
                    (sql % tuple(map(self.quote_value, params))) + ending
                )
            else:
                self.collected_sql.append(sql + ending)
        else:
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)

    def quote_name(self, name):
        return self.connection.ops.quote_name(name)

    def table_sql(self, model):
        """Take a model and return its table definition."""
        # Add any unique_togethers (always deferred, as some fields might be
        # created afterward, like geometry fields with some backends).
        for field_names in model._meta.unique_together:
            fields = [model._meta.get_field(field) for field in field_names]
            self.deferred_sql.append(self._create_unique_sql(model, fields))
        # Create column SQL, add FK deferreds if needed.
        column_sqls = []
        params = []
        for field in model._meta.local_fields:
            # SQL.
            definition, extra_params = self.column_sql(model, field)
            if definition is None:
                continue
            # Check constraints can go on the column SQL here.
            db_params = field.db_parameters(connection=self.connection)
            if db_params["check"]:
                definition += " " + self.sql_check_constraint % db_params
            # Autoincrement SQL (for backends with inline variant).
            col_type_suffix = field.db_type_suffix(connection=self.connection)
            if col_type_suffix:
                definition += " %s" % col_type_suffix
            params.extend(extra_params)
            # FK.
            if field.remote_field and field.db_constraint:
                to_table = field.remote_field.model._meta.db_table
                to_column = field.remote_field.model._meta.get_field(
                    field.remote_field.field_name
                ).column
                if self.sql_create_inline_fk:
                    definition += " " + self.sql_create_inline_fk % {
                        "to_table": self.quote_name(to_table),
                        "to_column": self.quote_name(to_column),
                    }
                elif self.connection.features.supports_foreign_keys:
                    self.deferred_sql.append(
                        self._create_fk_sql(
                            model, field, "_fk_%(to_table)s_%(to_column)s"
                        )
                    )
            # Add the SQL to our big list.
            column_sqls.append(
                "%s %s"
                % (
                    self.quote_name(field.column),
                    definition,
                )
            )
            # Autoincrement SQL (for backends with post table definition
            # variant).
            if field.get_internal_type() in (
                "AutoField",
                "BigAutoField",
                "SmallAutoField",
            ):
                autoinc_sql = self.connection.ops.autoinc_sql(
                    model._meta.db_table, field.column
                )
                if autoinc_sql:
                    self.deferred_sql.extend(autoinc_sql)
        constraints = [
            constraint.constraint_sql(model, self)
            for constraint in model._meta.constraints
        ]
        sql = self.sql_create_table % {
            "table": self.quote_name(model._meta.db_table),
            "definition": ", ".join(
                constraint for constraint in (*column_sqls, *constraints) if constraint
            ),
        }
        if model._meta.db_tablespace:
            tablespace_sql = self.connection.ops.tablespace_sql(
                model._meta.db_tablespace
            )
            if tablespace_sql:
                sql += " " + tablespace_sql
        return sql, params

    # Field <-> database mapping functions

    def _iter_column_sql(
        self, column_db_type, params, model, field, field_db_params, include_default
    ):
        yield column_db_type
        if collation := field_db_params.get("collation"):
            yield self._collate_sql(collation)
        # Work out nullability.
        null = field.null
        # Include a default value, if requested.
        include_default = (
            include_default
            and not self.skip_default(field)
            and
            # Don't include a default value if it's a nullable field and the
            # default cannot be dropped in the ALTER COLUMN statement (e.g.
            # MySQL longtext and longblob).
            not (null and self.skip_default_on_alter(field))
        )
        if include_default:
            default_value = self.effective_default(field)
            if default_value is not None:
                column_default = "DEFAULT " + self._column_default_sql(field)
                if self.connection.features.requires_literal_defaults:
                    # Some databases can't take defaults as a parameter (Oracle).
                    # If this is the case, the individual schema backend should
                    # implement prepare_default().
                    yield column_default % self.prepare_default(default_value)
                else:
                    yield column_default
                    params.append(default_value)
        # Oracle treats the empty string ('') as null, so coerce the null
        # option whenever '' is a possible value.
        if (
            field.empty_strings_allowed
            and not field.primary_key
            and self.connection.features.interprets_empty_strings_as_nulls
        ):
            null = True
        if not null:
            yield "NOT NULL"
        elif not self.connection.features.implied_column_null:
            yield "NULL"
        if field.primary_key:
            yield "PRIMARY KEY"
        elif field.unique:
            yield "UNIQUE"
        # Optionally add the tablespace if it's an implicitly indexed column.
        tablespace = field.db_tablespace or model._meta.db_tablespace
        if (
            tablespace
            and self.connection.features.supports_tablespaces
            and field.unique
        ):
            yield self.connection.ops.tablespace_sql(tablespace, inline=True)

    def column_sql(self, model, field, include_default=False):
        """
        Return the column definition for a field. The field must already have
        had set_attributes_from_name() called.
        """
        # Get the column's type and use that as the basis of the SQL.
        field_db_params = field.db_parameters(connection=self.connection)
        column_db_type = field_db_params["type"]
        # Check for fields that aren't actually columns (e.g. M2M).
        if column_db_type is None:
            return None, None
        params = []
        return (
            " ".join(
                # This appends to the params being returned.
                self._iter_column_sql(
                    column_db_type,
                    params,
                    model,
                    field,
                    field_db_params,
                    include_default,
                )
            ),
            params,
