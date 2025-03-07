# -*- coding: utf-8 -*-
import logging
import os
import re
from contextlib import contextmanager
from operator import itemgetter

import lxml
from psycopg2.extras import Json, execute_values

try:
    from odoo import release
    from odoo.tools.convert import xml_import
    from odoo.tools.misc import file_open
    from odoo.tools.translate import xml_translate
except ImportError:
    from openerp import release
    from openerp.tools.convert import xml_import
    from openerp.tools.misc import file_open

from .const import NEARLYWARN
from .exceptions import MigrationError
from .helpers import _get_theme_models, _ir_values_value, _validate_model, model_of_table, table_of_model
from .indirect_references import indirect_references
from .inherit import direct_inherit_parents, for_each_inherit
from .misc import parse_version, version_gte
from .orm import env
from .pg import (
    PGRegexp,
    _get_unique_indexes_with,
    _validate_table,
    column_exists,
    column_type,
    column_updatable,
    explode_execute,
    explode_query_range,
    get_columns,
    get_fk,
    get_value_or_en_translation,
    parallel_execute,
    table_exists,
    target_of,
)
from .report import add_to_migration_reports

_logger = logging.getLogger(__name__)

# python3 shims
try:
    basestring  # noqa: B018
except NameError:
    basestring = unicode = str


def remove_view(cr, xml_id=None, view_id=None, silent=False, key=None):
    """
    Recursively delete the given view and its inherited views, as long as they
    are part of a module. Will crash as soon as a custom view exists anywhere
    in the hierarchy.

    Also handle multi-website COWed views.
    """
    assert bool(xml_id) ^ bool(view_id)
    if xml_id:
        view_id = ref(cr, xml_id)
        if view_id:
            module, _, name = xml_id.partition(".")
            cr.execute("SELECT model FROM ir_model_data WHERE module=%s AND name=%s", [module, name])

            [model] = cr.fetchone()
            if model != "ir.ui.view":
                raise ValueError("%r should point to a 'ir.ui.view', not a %r" % (xml_id, model))
    else:
        # search matching xmlid for logging or renaming of custom views
        xml_id = "?"
        if not key:
            cr.execute("SELECT module, name FROM ir_model_data WHERE model='ir.ui.view' AND res_id=%s", [view_id])
            if cr.rowcount:
                xml_id = "%s.%s" % cr.fetchone()

    # From given or determined xml_id, the views duplicated in a multi-website
    # context are to be found and removed.
    if xml_id != "?" and column_exists(cr, "ir_ui_view", "key"):
        cr.execute("SELECT id FROM ir_ui_view WHERE key = %s AND id != %s", [xml_id, view_id])
        for [v_id] in cr.fetchall():
            remove_view(cr, view_id=v_id, silent=silent, key=xml_id)

    if not view_id:
        return

    cr.execute(
        """
        SELECT v.id, x.module || '.' || x.name, v.name
        FROM ir_ui_view v LEFT JOIN
           ir_model_data x ON (v.id = x.res_id AND x.model = 'ir.ui.view' AND x.module !~ '^_')
        WHERE v.inherit_id = %s;
    """,
        [view_id],
    )
    for child_id, child_xml_id, child_name in cr.fetchall():
        if child_xml_id:
            if not silent:
                _logger.info(
                    "remove deprecated built-in view %s (ID %s) as parent view %s (ID %s) is going to be removed",
                    child_xml_id,
                    child_id,
                    xml_id,
                    view_id,
                )
            remove_view(cr, child_xml_id, silent=True)
        else:
            if not silent:
                _logger.warning(
                    "deactivate deprecated custom view with ID %s as parent view %s (ID %s) is going to be removed",
                    child_id,
                    xml_id,
                    view_id,
                )
            disable_view_query = """
                UPDATE ir_ui_view
                SET name = (name || ' - old view, inherited from ' || %%s),
                    inherit_id = NULL
                    %s
                    WHERE id = %%s
            """
            # In 8.0, disabling requires setting mode to 'primary'
            extra_set_sql = ""
            if column_exists(cr, "ir_ui_view", "mode"):
                extra_set_sql = ",  mode = 'primary' "

            # Column was not present in v7 and it's older version
            if column_exists(cr, "ir_ui_view", "active"):
                extra_set_sql += ", active = false "

            disable_view_query = disable_view_query % extra_set_sql
            cr.execute(disable_view_query, (key or xml_id, child_id))
            add_to_migration_reports(
                {"id": child_id, "name": child_name},
                "Disabled views",
            )
    if not silent:
        _logger.info("remove deprecated %s view %s (ID %s)", key and "COWed" or "built-in", key or xml_id, view_id)

    remove_records(cr, "ir.ui.view", [view_id])


@contextmanager
def edit_view(cr, xmlid=None, view_id=None, skip_if_not_noupdate=True, active=True):
    """Contextmanager that may yield etree arch of a view.
    As it may not yield, you must use `skippable_cm`

        with util.skippable_cm(), util.edit_view(cr, 'xml.id') as arch:
            arch.attrib['string'] = 'My Form'

    When view_id is passed to identify a view, view's arch will always yield to be edited because
    we assume that xmlid for such view does not exist to check its noupdate flag.

    If view's noupdate=false then the arch will not be yielded for edit unless skip_if_not_noupdate=False,
    because when noupdate=False we assume it is a standard view that will be updated by the ORM later on anyways.

    If view's noupdate=True, the view will be yielded for edit.

    If the `active` argument is not None, the view will be (de)activated accordingly.

    For more details, see discussion in: https://github.com/odoo/upgrade-specific/pull/4216
    """
    assert bool(xmlid) ^ bool(view_id), "You Must specify either xmlid or view_id"
    noupdate = True
    if xmlid:
        if "." not in xmlid:
            raise ValueError("Please use fully qualified name <module>.<name>")

        module, _, name = xmlid.partition(".")
        cr.execute(
            """
                SELECT res_id, noupdate
                  FROM ir_model_data
                 WHERE module = %s
                   AND name = %s
        """,
            [module, name],
        )
        data = cr.fetchone()
        if data:
            view_id, noupdate = data

    if view_id and not (skip_if_not_noupdate and not noupdate):
        arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
        jsonb_column = column_type(cr, "ir_ui_view", arch_col) == "jsonb"
        cr.execute(
            """
                SELECT {arch}
                  FROM ir_ui_view
                 WHERE id=%s
            """.format(
                arch=arch_col,
            ),
            [view_id],
        )
        [arch] = cr.fetchone() or [None]
        if arch:

            def parse(arch):
                arch = arch.encode("utf-8") if isinstance(arch, unicode) else arch
                return lxml.etree.fromstring(arch.replace(b"&#13;\n", b"\n"))

            if jsonb_column:

                def get_trans_terms(value):
                    terms = []
                    xml_translate(terms.append, value)
                    return terms

                translation_terms = {lang: get_trans_terms(value) for lang, value in arch.items()}
                arch_etree = parse(arch["en_US"])
                yield arch_etree
                new_arch = lxml.etree.tostring(arch_etree, encoding="unicode")
                terms_en = translation_terms["en_US"]
                arch_column_value = Json(
                    {
                        lang: xml_translate(dict(zip(terms_en, terms)).get, new_arch)
                        for lang, terms in translation_terms.items()
                    }
                )
            else:
                arch_etree = parse(arch)
                yield arch_etree
                arch_column_value = lxml.etree.tostring(arch_etree, encoding="unicode")

            set_active = ", active={}".format(bool(active)) if active is not None else ""
            cr.execute(
                "UPDATE ir_ui_view SET {arch}=%s{set_active} WHERE id=%s".format(arch=arch_col, set_active=set_active),
                [arch_column_value, view_id],
            )


def add_view(cr, name, model, view_type, arch_db, inherit_xml_id=None, priority=16):
    inherit_id = None
    if inherit_xml_id:
        inherit_id = ref(cr, inherit_xml_id)
        if not inherit_id:
            raise ValueError(
                "Unable to add view '%s' because its inherited view '%s' cannot be found!" % (name, inherit_xml_id)
            )
    arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
    jsonb_column = column_type(cr, "ir_ui_view", arch_col) == "jsonb"
    arch_column_value = Json({"en_US": arch_db}) if jsonb_column else arch_db
    cr.execute(
        """
        INSERT INTO ir_ui_view(name, "type",  model, inherit_id, mode, active, priority, %s)
        VALUES(%%(name)s, %%(view_type)s, %%(model)s, %%(inherit_id)s, %%(mode)s, 't', %%(priority)s, %%(arch_db)s)
        RETURNING id
    """
        % arch_col,
        {
            "name": name,
            "view_type": view_type,
            "model": model,
            "inherit_id": inherit_id,
            "mode": "extension" if inherit_id else "primary",
            "priority": priority,
            "arch_db": arch_column_value,
        },
    )
    return cr.fetchone()[0]


# fmt:off
if version_gte("saas~14.3"):
    def remove_asset(cr, name):
        cr.execute("SELECT id FROM ir_asset WHERE bundle = %s", [name])
        if cr.rowcount:
            remove_records(cr, "ir.asset", [aid for aid, in cr.fetchall()])
else:
    def remove_asset(cr, name):
        remove_view(cr, name, silent=True)
# fmt:on


def remove_record(cr, name):
    if isinstance(name, basestring):
        if "." not in name:
            raise ValueError("Please use fully qualified name <module>.<name>")
        module, _, name = name.partition(".")
        cr.execute(
            """
                SELECT model, res_id
                  FROM ir_model_data
                 WHERE module = %s
                   AND name = %s
        """,
            [module, name],
        )
        if not cr.rowcount:
            return None
        model, res_id = cr.fetchone()
    elif isinstance(name, tuple):
        if len(name) != 2:
            raise ValueError("Please use a 2-tuple (<model>, <res_id>)")
        model, res_id = name
    else:
        raise TypeError("Either use a fully qualified xmlid string <module>.<name> or a 2-tuple (<model>, <res_id>)")

    # deleguate to the right method
    if model == "ir.ui.view":
        _logger.log(NEARLYWARN, "Removing view %r", name)
        return remove_view(cr, view_id=res_id)

    if model == "ir.ui.menu":
        _logger.log(NEARLYWARN, "Removing menu %r", name)
        return remove_menus(cr, [res_id])

    if model == "res.groups":
        _logger.log(NEARLYWARN, "Removing group %r", name)
        return remove_group(cr, group_id=res_id)

    return remove_records(cr, model, [res_id])


def remove_records(cr, model, ids):
    if not ids:
        return

    ids = tuple(ids)

    # remove theme model's copy_ids
    theme_copy_model = _get_theme_models().get(model)
    if theme_copy_model:
        cr.execute(
            'SELECT id FROM "{}" WHERE theme_template_id IN %s'.format(table_of_model(cr, theme_copy_model)),
            [ids],
        )
        if theme_copy_model == "ir.ui.view":
            for (view_id,) in cr.fetchall():
                remove_view(cr, view_id=view_id)
        else:
            remove_records(cr, theme_copy_model, [rid for rid, in cr.fetchall()])

    for inh in for_each_inherit(cr, model, skip=()):
        if inh.via:
            table = table_of_model(cr, inh.model)
            if not column_exists(cr, table, inh.via):
                # column may not exists in case of a partially unintalled module that left only *magic columns* in tables
                continue
            cr.execute('SELECT id FROM "{}" WHERE "{}" IN %s'.format(table, inh.via), [ids])
            if inh.model == "ir.ui.menu":
                remove_menus(cr, [menu_id for menu_id, in cr.fetchall()])
            elif inh.model == "ir.ui.view":
                for (view_id,) in cr.fetchall():
                    remove_view(cr, view_id=view_id)
            else:
                remove_records(cr, inh.model, [rid for rid, in cr.fetchall()])

    table = table_of_model(cr, model)
    cr.execute('DELETE FROM "{}" WHERE id IN %s'.format(table), [ids])
    for ir in indirect_references(cr, bound_only=True):
        query = 'DELETE FROM "{}" WHERE {} AND "{}" IN %s'.format(ir.table, ir.model_filter(), ir.res_id)
        cr.execute(query, [model, ids])
    _rm_refs(cr, model, ids)

    if model == "res.groups":
        # A group is gone, the auto-generated view `base.user_groups_view` is outdated.
        # Create a shim. It will be re-generated later by creating/updating groups or
        # explicitly in `base/0.0.0/end-user_groups_view.py`.
        arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
        jsonb_column = column_type(cr, "ir_ui_view", arch_col) == "jsonb"
        arch_value = "json_build_object('en_US', '<form/>')" if jsonb_column else "'<form/>'"
        cr.execute(
            "UPDATE ir_ui_view SET {} = {} WHERE id = %s".format(arch_col, arch_value),
            [ref(cr, "base.user_groups_view")],
        )


def _rm_refs(cr, model, ids=None):
    if ids is None:
        match = "like %s"
        needle = model + ",%"
    else:
        if not ids:
            return
        match = "in %s"
        needle = tuple("{0},{1}".format(model, i) for i in ids)

    # "model-comma" fields
    cr.execute(
        """
        SELECT model, name
          FROM ir_model_fields
         WHERE ttype='reference'
         UNION
        SELECT 'ir.translation', 'name'
    """
    )

    for ref_model, ref_column in cr.fetchall():
        table = table_of_model(cr, ref_model)
        if column_updatable(cr, table, ref_column):
            query_tail = ' FROM "{0}" WHERE "{1}" {2}'.format(table, ref_column, match)
            if ref_model == "ir.ui.view":
                cr.execute("SELECT id" + query_tail, [needle])
                for (view_id,) in cr.fetchall():
                    remove_view(cr, view_id=view_id, silent=True)
            elif ref_model == "ir.ui.menu":
                cr.execute("SELECT id" + query_tail, [needle])
                menu_ids = tuple(m[0] for m in cr.fetchall())
                remove_menus(cr, menu_ids)
            else:
                cr.execute("SELECT id" + query_tail, [needle])
                for (record_id,) in cr.fetchall():
                    remove_record(cr, (ref_model, record_id))

    if table_exists(cr, "ir_values"):
        column, _ = _ir_values_value(cr)
        query = "DELETE FROM ir_values WHERE {0} {1}".format(column, match)
        cr.execute(query, [needle])

    if ids is None and table_exists(cr, "ir_translation"):
        cr.execute(
            """
            DELETE FROM ir_translation
             WHERE name=%s
               AND type IN ('constraint', 'sql_constraint', 'view', 'report', 'rml', 'xsl')
        """,
            [model],
        )


def is_changed(cr, xmlid, interval="1 minute"):
    """
    This utility will return a false positive on xmlids of records that match the following conditions:
        * Have been updated in an upgrade preceding the current one
        * Have not been updated in the current upgrade
    """
    assert "." in xmlid
    module, _, name = xmlid.partition(".")
    cr.execute("SELECT model, res_id FROM ir_model_data WHERE module=%s AND name=%s", [module, name])
    data = cr.fetchone()
    if not data:
        return None
    model, res_id = data
    table = table_of_model(cr, model)
    cr.execute(
        """
        SELECT 1
          FROM {} r
     LEFT JOIN ir_config_parameter p
            ON p.key = 'upgrade.start.time'
         WHERE r.id = %s
           -- Note: use a negative search to handle the case of NULL values in write/create_date
           AND COALESCE(r.write_date < p.value::timestamp, True)
           AND r.write_date - r.create_date > interval %s
        """.format(
            table
        ),
        [res_id, interval],
    )
    return bool(cr.rowcount)


def if_unchanged(cr, xmlid, callback, interval="1 minute", **kwargs):
    if not is_changed(cr, xmlid, interval=interval):
        callback(cr, xmlid, **kwargs)
    else:
        force_noupdate(cr, xmlid, noupdate=True)


def remove_menus(cr, menu_ids):
    if not menu_ids:
        return
    cr.execute(
        """
        WITH RECURSIVE tree(id) AS (
            SELECT id
              FROM ir_ui_menu
             WHERE id IN %s
             UNION
            SELECT m.id
              FROM ir_ui_menu m
              JOIN tree t ON (m.parent_id = t.id)
        )
        DELETE FROM ir_ui_menu m
              USING tree t
              WHERE m.id = t.id
          RETURNING m.id
    """,
        [tuple(menu_ids)],
    )
    ids = tuple(x[0] for x in cr.fetchall())
    if ids:
        cr.execute("DELETE FROM ir_model_data WHERE model='ir.ui.menu' AND res_id IN %s", [ids])


def remove_group(cr, xml_id=None, group_id=None):
    assert bool(xml_id) ^ bool(group_id)
    if xml_id:
        group_id = ref(cr, xml_id)
        if group_id:
            module, _, name = xml_id.partition(".")
            cr.execute("SELECT model FROM ir_model_data WHERE module=%s AND name=%s", [module, name])
            [model] = cr.fetchone()
            if model != "res.groups":
                raise ValueError("%r should point to a 'res.groups', not a %r" % (xml_id, model))

    if not group_id:
        return

    # Get all fks from table res_groups
    fks = get_fk(cr, "res_groups", quote_ident=False)

    # Remove records referencing the group_id from the referencing tables (restrict fks)
    standard_tables = ["ir_model_access", "rule_group_rel"]
    custom_tables = []
    for foreign_table, foreign_column, _, on_delete_action in fks:
        if on_delete_action == "r":
            if foreign_table not in standard_tables:
                cr.execute(
                    'SELECT COUNT(*) FROM "{}" WHERE "{}" = %s'.format(foreign_table, foreign_column),
                    (group_id,),
                )
                count = cr.fetchone()[0]
                if count:
                    custom_tables.append((foreign_table, foreign_column, count))
                continue

            query = 'DELETE FROM "{}" WHERE "{}" = %s'.format(foreign_table, foreign_column)
            query = cr.mogrify(query, (group_id,)).decode()

            if column_exists(cr, foreign_table, "id"):
                parallel_execute(cr, explode_query_range(cr, query, table=foreign_table))
            else:
                cr.execute(query)

    if custom_tables:
        col_name = get_value_or_en_translation(cr, "res_groups", "name")
        cr.execute("SELECT {} FROM res_groups WHERE id = %s".format(col_name), [group_id])
        group_name = cr.fetchone()[0]
        raise MigrationError(
            "\nThe following 'table (column) - records count' are referencing the group '{}'".format(group_name)
            + " and cannot be removed automatically:\n"
            + "\n".join(
                " - {} ({}) - {} record(s)".format(table, column, count) for table, column, count in custom_tables
            )
            + "\nPlease remove them manually or remove the foreign key constraints set as RESTRICT."
        )

    remove_records(cr, "res.groups", [group_id])


def rename_xmlid(cr, old, new, noupdate=None, on_collision="fail"):
    if "." not in old or "." not in new:
        raise ValueError("Please use fully qualified name <module>.<name>")
    if on_collision not in {"fail", "merge"}:
        raise ValueError("Invalid value for the `on_collision` argument: {0!r}".format(on_collision))
    if old == new:
        raise ValueError("Cannot rename an XMLID to itself")

    old_module, _, old_name = old.partition(".")
    new_module, _, new_name = new.partition(".")
    cr.execute("SELECT model, res_id FROM ir_model_data WHERE module = %s AND name = %s", [new_module, new_name])
    new_model, new_id = cr.fetchone() or (None, None)
    cr.execute("SELECT model, res_id FROM ir_model_data WHERE module = %s AND name = %s", [old_module, old_name])
    model, old_id = cr.fetchone() or (None, None)

    if new_id and old_id:
        if (model, old_id) != (new_model, new_id):
            if on_collision == "fail":
                raise MigrationError("Can't rename {} to {} as it already exists".format(old, new))

            if model != new_model:
                raise MigrationError("Model mismatch while renaming xmlid {}. {} to {}".format(old, model, new_model))

            replace_record_references(cr, (model, old_id), (model, new_id), replace_xmlid=False)

        if noupdate is not None:
            force_noupdate(cr, new, bool(noupdate))
        cr.execute("DELETE FROM ir_model_data WHERE module=%s AND name=%s", [old_module, old_name])
    else:
        nu = "" if noupdate is None else (", noupdate=" + str(bool(noupdate)).lower())

        cr.execute(
            """UPDATE ir_model_data
                  SET module=%s, name=%s
                   {}
                WHERE module=%s AND name=%s
            RETURNING model, res_id
            """.format(
                nu
            ),
            (new_module, new_name, old_module, old_name),
        )
        model, new_id = cr.fetchone() or (None, None)

    if model and new_id:
        if model == "ir.ui.view" and column_exists(cr, "ir_ui_view", "key"):
            cr.execute("UPDATE ir_ui_view SET key=%s WHERE id=%s AND key=%s", [new, new_id, old])
            if cr.rowcount:
                # iif the key has been updated for this view, also update it for all other cowed views.
                # Don't change the view keys inconditionally to avoid changing unrelated views.
                cr.execute("UPDATE ir_ui_view SET key = %s WHERE key = %s", [new, old])

        for parent_model, inh in direct_inherit_parents(cr, model):
            if inh.via:
                parent = parent_model.replace(".", "_")
                rename_xmlid(
                    cr,
                    "{}_{}".format(old, parent),
                    "{}_{}".format(new, parent),
                    noupdate=noupdate,
                    on_collision=on_collision,
                )
        return new_id
    return None


def ref(cr, xmlid):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            SELECT res_id
              FROM ir_model_data
             WHERE module = %s
               AND name = %s
    """,
        [module, name],
    )
    data = cr.fetchone()
    if data:
        return data[0]
    return None


def force_noupdate(cr, xmlid, noupdate=True, warn=False):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            UPDATE ir_model_data
               SET noupdate = %s
             WHERE module = %s
               AND name = %s
               AND noupdate != %s
    """,
        [noupdate, module, name, noupdate],
    )
    if noupdate is False and cr.rowcount and warn:
        _logger.warning("Customizations on `%s` might be lost!", xmlid)
    return cr.rowcount


def ensure_xmlid_match_record(cr, xmlid, model, values):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    logger = _logger.getChild("ensure_xmlid_match_record")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            SELECT id, res_id
              FROM ir_model_data
             WHERE module = %s
               AND name = %s
    """,
        [module, name],
    )
    data_id, res_id = cr.fetchone() or (None, None)

    table = table_of_model(cr, model)

    # search for existing records matching values
    where = []
    data = ()
    for k, v in values.items():
        if v is not None:
            where += ["%s = %%s" % (get_value_or_en_translation(cr, table, k),)]
            data += (v,)
        else:
            where += ["%s IS NULL" % (k,)]
            data += ()

    query = ("SELECT id FROM %s WHERE " % table) + " AND ".join(where)
    cr.execute(query, data)
    records = [id for id, in cr.fetchall()]
    if res_id and res_id in records:
        return res_id
    if not records:
        if res_id:
            logger.debug("`%s` refers %s(%s); values differ %r; no other match found.", xmlid, model, res_id, values)
            return res_id

        logger.debug("`%s` doesn't exist; no match found for values %r", xmlid, values)
        return None
    new_res_id = records[0]

    if data_id:
        logger.info("update `%s` from %s(%s) to %s(%s); values %r", xmlid, model, new_res_id, model, res_id, values)
        cr.execute(
            """
                UPDATE ir_model_data
                   SET res_id=%s
                 WHERE id=%s
        """,
            [new_res_id, data_id],
        )
    else:
        logger.info("create `%s` that point to %s(%s); matching values %r", xmlid, model, new_res_id, values)
        cr.execute(
            """
                INSERT INTO ir_model_data(module, name, model, res_id, noupdate)
                     VALUES (%s, %s, %s, %s, %s)
        """,
            [module, name, model, new_res_id, True],
        )

    return new_res_id


def update_record_from_xml(
    cr,
    xmlid,
    reset_write_metadata=True,
    force_create=False,
    from_module=None,
    reset_translations=(),
    ensure_references=False,
):
    __update_record_from_xml(
        cr,
        xmlid,
        reset_write_metadata=reset_write_metadata,
        force_create=force_create,
        from_module=from_module,
        reset_translations=reset_translations,
        ensure_references=ensure_references,
        done_refs=set(),
    )


def __update_record_from_xml(
    cr,
    xmlid,
    reset_write_metadata,
    force_create,
    from_module,
    reset_translations,
    ensure_references,
    done_refs,
):
    from .modules import get_manifest

    # Force update of a record from xml file to bypass the noupdate flag
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")

    cr.execute(
        """
        UPDATE ir_model_data d
           SET noupdate = false
          FROM ir_model_data o
         WHERE o.id = d.id
           AND d.module = %s
           AND d.name = %s
     RETURNING d.model, d.res_id, o.noupdate
    """,
        [module, name],
    )
    if cr.rowcount:
        model, res_id, noupdate = cr.fetchone()
    elif not force_create:
        return
    else:
        # The xmlid doesn't already exists, nothing to reset
        reset_write_metadata = noupdate = reset_translations = False

    write_data = None
    if reset_write_metadata:
        table = table_of_model(cr, model)
        cr.execute("SELECT write_uid, write_date, id FROM {} WHERE id=%s".format(table), [res_id])
        write_data = cr.fetchone()

    from_module = from_module or module

    id_match = (
        "@id='{module}.{name}' or @id='{name}'".format(module=module, name=name)
        if module == from_module
        else "@id='{module}.{name}'".format(module=module, name=name)
    )
    xpath = "//*[self::act_window or self::menuitem or self::record or self::report or self::template][{}]".format(id_match)  # fmt: skip

    # use a data tag inside openerp tag to be compatible with all supported versions
    new_root = lxml.etree.fromstring("<openerp><data/></openerp>")

    manifest = get_manifest(from_module)
    template = False
    extra_references = []

    def add_ref(ref):
        if "." not in ref:
            extra_references.append(from_module + "." + ref)
        elif ref.split(".")[0] == from_module:
            extra_references.append(ref)

    for f in manifest.get("data", []):
        if not f.endswith(".xml"):
            continue
        with file_open(os.path.join(from_module, f)) as fp:
            doc = lxml.etree.parse(fp)
            for node in doc.xpath(xpath):
                parent = node.getparent()
                new_root[0].append(node)

                if node.tag == "menuitem" and parent.tag == "menuitem" and "parent_id" not in node.attrib:
                    new_root[0].append(
                        lxml.builder.E.record(
                            lxml.builder.E.field(name="parent_id", ref=parent.attrib["id"]),
                            model="ir.ui.menu",
                            id=node.attrib["id"],
                        )
                    )

                if node.tag == "template":
                    template = True
                if ensure_references:
                    for ref_node in node.xpath("//field[@ref]"):
                        add_ref(ref_node.get("ref"))
                    for eval_node in node.xpath("//field[@eval]"):
                        for ref_match in re.finditer(r"\bref\((['\"])(.*?)\1\)", eval_node.get("eval")):
                            add_ref(ref_match.group(2))

    done_refs.add(xmlid)
    for ref in extra_references:
        if ref in done_refs:
            continue
        _logger.info("Update of %s - ensuring the reference %s exists", xmlid, ref)
        __update_record_from_xml(
            cr,
            ref,
            reset_write_metadata=reset_write_metadata,
            force_create=True,
            from_module=from_module,
            reset_translations=reset_translations,
            ensure_references=True,
            done_refs=done_refs,
        )

    cr_or_env = env(cr) if version_gte("saas~16.2") else cr
    importer = xml_import(cr_or_env, from_module, idref={}, mode="update")
    kw = {"mode": "update"} if parse_version("8.0") <= parse_version(release.series) <= parse_version("12.0") else {}
    importer.parse(new_root, **kw)

    if noupdate:
        force_noupdate(cr, xmlid, noupdate=True)
    if reset_write_metadata and write_data:
        cr.execute("UPDATE {} SET write_uid=%s, write_date=%s WHERE id=%s".format(table), write_data)

    if reset_translations:
        if reset_translations is True:
            fields_with_values_from_xml = {elem.attrib["name"] for elem in node.xpath("//record/field")}
            if template:
                fields_with_values_from_xml |= {"arch_db", "name"}
            cr.execute(
                "SELECT name FROM ir_model_fields WHERE model = %s AND translate = true AND name IN %s",
                [model, tuple(fields_with_values_from_xml)],
            )
            reset_translations = [fname for [fname] in cr.fetchall()]

        if table_exists(cr, "ir_translation"):
            cr.execute(
                """
                    DELETE FROM ir_translation
                          WHERE name IN %s
                            AND res_id = %s
                """,
                [tuple("{},{}".format(model, f) for f in reset_translations), res_id],
            )
        else:
            query = """
                UPDATE {}
                   SET {}
                 WHERE id = %s
            """.format(
                table,
                ",".join(
                    [
                        """%s = NULLIF(jsonb_build_object('en_US', %s->>'en_US'), '{"en_US": null}'::jsonb)"""
                        % (fname, fname)
                        for fname in reset_translations
                    ]
                ),
            )
            cr.execute(query, [res_id])

        env_ = env(cr)
        module_to_reload_from = env_["ir.module.module"].search(
            [("name", "=", from_module), ("state", "=", "installed")]
        )
        if module_to_reload_from:
            if hasattr(module_to_reload_from, "_update_translations"):
                module_to_reload_from._update_translations()
            else:
                # < 9.0
                module_to_reload_from.update_translations()


def delete_unused(cr, *xmlids, **kwargs):
    deactivate = kwargs.pop("deactivate", False)
    if kwargs:
        raise TypeError("delete_unused() got an unexpected keyword argument %r" % kwargs.popitem()[0])

    select_xids = " UNION ".join(
        [
            cr.mogrify("SELECT %s::varchar as module, %s::varchar as name", [module, name]).decode()
            for xmlid in xmlids
            for module, _, name in [xmlid.partition(".")]
        ]
    )

    cr.execute(
        """
       WITH xids AS (
         {}
       ),
       _upd AS (
            UPDATE ir_model_data d
               SET noupdate = true
              FROM xids x
             WHERE d.module = x.module
               AND d.name = x.name
         RETURNING d.id, d.model, d.res_id, d.module || '.' || d.name as xmlid
       )
       SELECT model, array_agg(res_id ORDER BY id), array_agg(xmlid ORDER BY id)
         FROM _upd
     GROUP BY model
    """.format(
            select_xids
        )
    )

    deleted = []
    for model, ids, xmlids in cr.fetchall():
        table = table_of_model(cr, model)
        res_id_to_xmlid = dict(zip(ids, xmlids))

        sub = " UNION ".join(
            [
                'SELECT 1 FROM "{}" x WHERE x."{}" = t.id'.format(fk_tbl, fk_col)
                for fk_tbl, fk_col, _, fk_act in get_fk(cr, table, quote_ident=False)
                # ignore "on delete cascade" fk (they are indirect dependencies (lines or m2m))
                if fk_act != "c"
                # ignore children records unless the deletion is restricted
                if not (fk_tbl == table and fk_act != "r")
            ]
        )
        if sub:
            cr.execute(
                """
                SELECT id
                  FROM "{}" t
                 WHERE id = ANY(%s)
                   AND NOT EXISTS({})
            """.format(
                    table, sub
                ),
                [list(ids)],
            )
            ids = map(itemgetter(0), cr.fetchall())  # noqa: PLW2901

        ids = list(ids)  # noqa: PLW2901
        if model == "res.lang" and table_exists(cr, "ir_translation"):
            cr.execute(
                """
                DELETE FROM ir_translation t
                      USING res_lang l
                      WHERE t.lang = l.code
                        AND l.id = ANY(%s)
                 """,
                [ids],
            )
        for tid in ids:
            remove_record(cr, (model, tid))
            deleted.append(res_id_to_xmlid[tid])

        if deactivate:
            deactivate_ids = tuple(set(res_id_to_xmlid.keys()) - set(ids))
            if deactivate_ids:
                cr.execute('UPDATE "{}" SET active = false WHERE id IN %s'.format(table), [deactivate_ids])

    return deleted


def replace_record_references(cr, old, new, replace_xmlid=True):
    """replace all (in)direct references of a record by another"""
    # TODO update workflow instances?
    assert isinstance(old, tuple) and len(old) == 2
    assert isinstance(new, tuple) and len(new) == 2

    if not old[1]:
        return None

    return replace_record_references_batch(cr, {old[1]: new[1]}, old[0], new[0], replace_xmlid)


def replace_record_references_batch(cr, id_mapping, model_src, model_dst=None, replace_xmlid=True, ignores=()):
    assert id_mapping
    assert all(isinstance(v, int) and isinstance(k, int) for k, v in id_mapping.items())

    _validate_model(model_src)
    if model_dst is None:
        model_dst = model_src
    else:
        _validate_model(model_dst)

    id_update = any(k != v for k, v in id_mapping.items())
    nop = (not id_update) and (model_src == model_dst)
    assert nop is False

    ignores = [_validate_table(table) for table in ignores]
    if not replace_xmlid:
        ignores.append("ir_model_data")

    cr.execute("CREATE UNLOGGED TABLE _upgrade_rrr(old int PRIMARY KEY, new int)")
    execute_values(cr, "INSERT INTO _upgrade_rrr (old, new) VALUES %s", id_mapping.items())

    if model_src == model_dst:
        fk_def = []

        model_src_table = table_of_model(cr, model_src)
        for table, fk, _, _ in get_fk(cr, model_src_table):
            if table in ignores:
                continue
            query = """
                UPDATE {table} t
                   SET {fk} = r.new
                  FROM _upgrade_rrr r
                 WHERE r.old = t.{fk}
            """

            if not column_exists(cr, table, "id"):
                # seems to be a m2m table. Avoid duplicated entries
                cols = get_columns(cr, table, ignore=(fk,))
                assert len(cols) == 1  # it's a m2, should have only 2 columns
                col2 = cols[0]
                query += """
                    AND NOT EXISTS(SELECT 1 FROM {table} e WHERE e.{col2} = t.{col2} AND e.{fk} = r.new);

                    DELETE
                      FROM {table} t
                     USING _upgrade_rrr r
                     WHERE t.{fk} = r.old;
                """

                col2_info = target_of(cr, table, col2)  # col2 may not be a FK
                if col2_info and col2_info[:2] == (model_src_table, "id"):
                    # a m2m on itself, remove the self referencing entries
                    # It only handle 1-level recursions. For multi-level recursions, it should be handled manually.
                    # We can't decide which link to break.
                    # XXX: add a warning?
                    query += """
                        DELETE
                          FROM {table} t
                         USING _upgrade_rrr r
                         WHERE t.{fk} = r.new
                           AND t.{fk} = t.{col2};
                    """

                cr.execute(query.format(table=table, fk=fk, col2=col2))

            else:  # it's a model
                fmt_query = cr.mogrify(query.format(table=table, fk=fk)).decode()
                parallel_execute(cr, explode_query_range(cr, fmt_query, table=table, alias="t"))

                # track default values to update
                model = model_of_table(cr, table)
                fk_def.append((model, fk))

        if fk_def:
            if table_exists(cr, "ir_values"):
                column_read, cast_write = _ir_values_value(cr, prefix="v")
                query = r"""
                    UPDATE ir_values v
                       SET value = {cast_write}
                      FROM _upgrade_rrr r
                     WHERE v.key = 'default'
                       AND {column_read} = format(E'I%%s\n.', r.old)
                       AND (v.model, v.name) IN %s
                """.format(
                    column_read=column_read, cast_write=cast_write % r"format(E'I%%s\n.', r.new)"
                )
            else:
                query = """
                    UPDATE ir_default d
                       SET json_value = r.new::varchar
                      FROM _upgrade_rrr r, ir_model_fields f
                     WHERE f.id = d.field_id
                       AND d.json_value = r.old::varchar
                       AND (f.model, f.name) IN %s
                """

            cr.execute(query, [tuple(fk_def)])

    cr.execute("SELECT id FROM ir_model WHERE model=%s", [model_dst])
    model_dest_id = cr.fetchone()[0]

    # indirect references
    for ir in indirect_references(cr, bound_only=True):
        if ir.table in ignores:
            continue
        res_model_upd = []
        if ir.res_model:
            res_model_upd.append('"{ir.res_model}" = %(model_dst)s')
        if ir.res_model_id:
            res_model_upd.append('"{ir.res_model_id}" = %(model_dest_id)s')
        upd = ", ".join(res_model_upd).format(ir=ir)
        res_model_whr = " AND ".join(res_model_upd).format(ir=ir)
        whr = ir.model_filter(placeholder="%(model_src)s")

        if not id_update:
            jmap_expr = "true"  # no-op
            jmap_expr_upd = ""
        else:
            jmap_expr = '"{ir.res_id}" = _upgrade_rrr.new'.format(ir=ir)
            jmap_expr_upd = ", " + jmap_expr

        query = """
            UPDATE "{ir.table}" t
               SET {upd}
                   {jmap_expr_upd}
              FROM _upgrade_rrr
             WHERE {whr}
               AND _upgrade_rrr.old = {ir.res_id}
        """

        unique_indexes = []
        if ir.res_model:
            unique_indexes += _get_unique_indexes_with(cr, ir.table, ir.res_id, ir.res_model)
        if ir.res_model_id:
            unique_indexes += _get_unique_indexes_with(cr, ir.table, ir.res_id, ir.res_model_id)
        if unique_indexes:
            conditions = []
            for _, uniq_cols in unique_indexes:
                uniq_cols = set(uniq_cols) - {ir.res_id, ir.res_model, ir.res_model_id}  # noqa: PLW2901
                conditions.append(
                    """
                        NOT EXISTS(SELECT 1 FROM {ir.table} WHERE {res_model_whr} AND {jmap_expr} AND %(ands)s)
                    """
                    % {"ands": "AND".join('"%s"=t."%s"' % (col, col) for col in uniq_cols)}
                )
            query = """
                    %s
                    AND %s;
                DELETE FROM {ir.table} USING _upgrade_rrr WHERE {whr} AND {ir.res_id} = _upgrade_rrr.old;
            """ % (
                query,
                "AND".join(conditions),
            )
            cr.execute(query.format(**locals()), locals())
        else:
            fmt_query = cr.mogrify(query.format(**locals()), locals()).decode()
            parallel_execute(cr, explode_query_range(cr, fmt_query, table=ir.table, alias="t"))

    # reference fields
    cr.execute("SELECT model, name FROM ir_model_fields WHERE ttype='reference'")
    for model, column in cr.fetchall():
        table = table_of_model(cr, model)
        if table not in ignores and column_updatable(cr, table, column):
            cr.execute(
                """
                    WITH _ref AS (
                        SELECT concat(%s, ',', old) as old, concat(%s, ',', new) as new
                          FROM _upgrade_rrr
                    )
                    UPDATE "{table}" t
                       SET "{column}" = r.new
                      FROM _ref r
                     WHERE t."{column}" = r.old
            """.format(
                    table=table, column=column
                ),
                [model_src, model_dst],
            )

    cr.execute("DROP TABLE _upgrade_rrr")


def replace_in_all_jsonb_values(cr, table, column, old, new, extra_filter=None):
    """
    Will replace `old` by `new` (whole word match) in all jsonb value of `column` of `table`.
    The `extra_filter` should use the `t` alias. It can also include `{parallel_filter}` to
    execute the query in parallel.
    `old` can be a simple term (str) or a regexp (util.PGRegexp)
    """
    re_old = (
        old
        if isinstance(old, PGRegexp)
        else "{}{}{}".format(
            r"\y" if re.match(r"\w", old[0]) else "",
            re.escape(old),
            r"\y" if re.match(r"\w", old[-1]) else "",
        )
    )
    match = str(Json(re_old))[1:-1]  # escapes re_old into json string

    if extra_filter is None:
        extra_filter = "true"

    query = cr.mogrify(
        """
        WITH upd AS (
             SELECT t.id,
                    jsonb_object_agg(v.key, regexp_replace(v.value, %s, %s, 'g')) AS value
               FROM "{table}" t
               JOIN LATERAL jsonb_each_text(t."{column}") v
                 ON true
              WHERE jsonb_path_match(t."{column}", 'exists($.* ? (@ like_regex {match}))')
                AND {extra_filter}
              GROUP BY t.id
        )
        UPDATE "{table}" t
           SET "{column}" = upd.value
          FROM upd
         WHERE upd.id = t.id
        """.format(
            **locals()
        ),
        [re_old, new],
    ).decode()

    if "{parallel_filter}" in query:
        explode_execute(cr, query, table=table, alias="t")
    else:
        cr.execute(query)


def ensure_mail_alias_mapping(cr, model, record_xmlid, alias_xmlid, alias_name):
    _validate_model(model)

    cr.execute("SELECT id FROM ir_model WHERE model = %s", [model])
    (model_id,) = cr.fetchone()
    alias_id = ensure_xmlid_match_record(
        cr,
        alias_xmlid,
        "mail.alias",
        {
            "alias_name": alias_name,
            "alias_parent_model_id": model_id,
        },
    )

    if alias_id:
        ensure_xmlid_match_record(
            cr,
            record_xmlid,
            model,
            {"alias_id": alias_id},
        )


def remove_act_window_view_mode(cr, model, view_mode):
    cr.execute(
        """
        WITH upd AS (
            UPDATE ir_act_window act
               SET view_mode = COALESCE(
                      NULLIF(
                          ARRAY_TO_STRING(ARRAY_REMOVE(STRING_TO_ARRAY(view_mode, ','), %s), ','),
                          '' -- invalid value
                      ),
                      'tree,form' -- default value
                   )
             WHERE act.res_model = %s
               AND %s = ANY(STRING_TO_ARRAY(act.view_mode, ','))
         RETURNING act.id

        )
        DELETE FROM ir_act_window_view av
              USING upd
              WHERE upd.id = av.act_window_id
                AND av.view_mode=%s
        """,
        [view_mode, model, view_mode, view_mode],
    )
