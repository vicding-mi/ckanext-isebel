# encoding: utf-8
from __future__ import annotations

import hashlib
import json
import logging
import inspect
import sys
from collections import OrderedDict
from datetime import datetime
from functools import partial
from typing_extensions import TypeAlias
from urllib.parse import urlencode
from typing import Any, Iterable, Optional, Union, cast

from flask import Blueprint
from flask.views import MethodView
from jinja2.exceptions import TemplateNotFound
from werkzeug.datastructures import MultiDict
from ckan.common import asbool, current_user

import ckan.lib.base as base
import ckan.lib.helpers as h
import ckan.lib.navl.dictization_functions as dict_fns
import ckan.logic as logic
import ckan.model as model
import ckan.plugins as plugins
import ckan.authz as authz
from ckan.common import _, config, g, request
from ckan.views.home import CACHE_PARAMETERS
from ckan.lib.plugins import lookup_package_plugin
from ckan.lib.search import SearchError, SearchQueryError, SearchIndexError
from ckan.types import Context, Response
import ckan.lib.redis as redis


NotFound = logic.NotFound
NotAuthorized = logic.NotAuthorized
ValidationError = logic.ValidationError
check_access = logic.check_access
get_action = logic.get_action
tuplize_dict = logic.tuplize_dict
clean_dict = logic.clean_dict
parse_params = logic.parse_params
flatten_to_string_key = logic.flatten_to_string_key

log = logging.getLogger(__name__)

bp = Blueprint(u'isebel', __name__)


def _setup_template_variables(context: Context,
                              data_dict: dict[str, Any],
                              package_type: Optional[str] = None) -> None:
    return lookup_package_plugin(package_type).setup_template_variables(
        context, data_dict
    )


def _get_pkg_template(template_type: str,
                      package_type: Optional[str] = None) -> str:
    pkg_plugin = lookup_package_plugin(package_type)
    method = getattr(pkg_plugin, template_type)
    signature = inspect.signature(method)
    if len(signature.parameters):
        return method(package_type)
    else:
        return method()


def _encode_params(params: Iterable[tuple[str, Any]]):
    return [(k, v.encode(u'utf-8') if isinstance(v, str) else str(v))
            for k, v in params]


Params: TypeAlias = "list[tuple[str, Any]]"


def url_with_params(url: str, params: Params) -> str:
    params = _encode_params(params)
    return url + u'?' + urlencode(params)


def search_url(params: Params, package_type: Optional[str] = None) -> str:
    if not package_type:
        package_type = u'dataset'
    url = h.url_for(u'{0}.search'.format(package_type))
    return url_with_params(url, params)


def remove_field(package_type: Optional[str],
                 key: str,
                 value: Optional[str] = None,
                 replace: Optional[str] = None):
    if not package_type:
        package_type = u'dataset'
    url = h.url_for(u'{0}.search'.format(package_type))
    return h.remove_url_param(
        key,
        value=value,
        replace=replace,
        alternative_url=url
    )


def _sort_by(params_nosort: Params, package_type: str,
             fields: Iterable[tuple[str, str]]) -> str:
    """Sort by the given list of fields.

    Each entry in the list is a 2-tuple: (fieldname, sort_order)
    eg - [(u'metadata_modified', u'desc'), (u'name', u'asc')]
    If fields is empty, then the default ordering is used.
    """
    params = params_nosort[:]

    if fields:
        sort_string = u', '.join(u'%s %s' % f for f in fields)
        params.append((u'sort', sort_string))
    return search_url(params, package_type)


def _pager_url(params_nopage: Params,
               package_type: str,
               q: Any = None,  # noqa
               page: Optional[int] = None) -> str:
    params = list(params_nopage)
    params.append((u'page', page))
    return search_url(params, package_type)


def _get_search_details() -> dict[str, Any]:
    fq = u''

    # fields_grouped will contain a dict of params containing
    # a list of values eg {u'tags':[u'tag1', u'tag2']}

    fields = []
    fields_grouped = {}
    search_extras: 'MultiDict[str, Any]' = MultiDict()

    for (param, value) in request.args.items(multi=True):
        if param not in [u'q', u'page', u'sort'] \
                and len(value) and not param.startswith(u'_'):
            if not param.startswith(u'ext_'):
                fields.append((param, value))
                fq += u' %s:"%s"' % (param, value)
                if param not in fields_grouped:
                    fields_grouped[param] = [value]
                else:
                    fields_grouped[param].append(value)
            else:
                search_extras.update({param: value})

    extras = dict([
        (k, v[0]) if len(v) == 1 else (k, v)
        for k, v in search_extras.lists()
    ])
    return {
        u'fields': fields,
        u'fields_grouped': fields_grouped,
        u'fq': fq,
        u'search_extras': extras,
    }


def facet_loadjson(orgstr, swap=True):
    '''return json obj'''
    json_data = json.loads(orgstr)
    if swap:
        if json_data['type'] == 'Point':
            json_data['coordinates'] = [[json_data['coordinates'][1], json_data['coordinates'][0]]]
        else:
            json_data['coordinates'] = map(lambda x: list(reversed(x)), reversed(json_data['coordinates']))
    return json_data


def get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, HARD_LIMIT):
    # log.debug('before loop: {}'.format(data_dict_full_result))
    query_full_result = get_action('package_search')(context, data_dict_full_result)
    full_results = list()
    while query_full_result.get('results', None) and pager < PAGER_LIMIT:
        full_results.extend(query_full_result.get('results', None))
        pager += 1

        data_dict_full_result['start'] = pager * HARD_LIMIT
        # log.info('in loop: {}'.format(data_dict_full_result))
        query_full_result = get_action('package_search')(context, data_dict_full_result)
        # log.info('result: {}'.format(query_full_result.get('results', None)))
    # log.info('full results: {}'.format(full_results))
    return full_results


def get_map_result(full_results):
    map_results = list()
    for r in full_results:
        for urlDict in r['extras']:
            if urlDict['key'] == 'identifier':
                current_url = urlDict['value']
        for spatial in r['extras']:
            if spatial['key'] == 'spatial':
                markers = facet_loadjson(spatial['value'])
                for geopoint in markers['coordinates']:
                    # map_results.append([geopoint[0], geopoint[1], r['name'], current_url])
                    map_results.append([geopoint[0], geopoint[1], r['name'], r['name']])
        # log.info('######################## {}'.format(r['name']))
    return map_results


def is_empty(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    else:
        return not value


def convert_to_unix_timestamp(time_to_check: datetime) -> float:
    return (time_to_check - datetime(1970, 1, 1)).total_seconds()


def delete_redis_keys(r: redis.Redis,
                      prefix: str = "ckanext_isebel:",
                      max_age: Optional[float] = None,
                      limit: Optional[int] = None,
                      ) -> None:
    """
    Delete keys from redis

    :param max_age:
    :param r: redis connection
    :param key: key to delete
    :param prefix: prefix of the keys to delete
    :param limit: limit the number of keys to delete
    :return:
    """
    clean_counter: int = 0
    keys = r.keys(f"{prefix}*".encode("utf-8"))
    keys = [key for key in keys if not key.endswith(b"_age")]

    if limit:
        keys = keys[:limit]
    for key in keys:
        if max_age is not None:
            if r.exists(key + b"_age") and convert_to_unix_timestamp(datetime.utcnow()) - float(r.get(key + b"_age")) > max_age:
                r.delete(key)
                r.delete(key + b"_age")
                clean_counter += 1
            elif not r.exists(key + b"_age"):
                r.delete(key)
                clean_counter += 1
        else:
            r.delete(key)
            r.delete(key + b"_age")
            clean_counter += 1
    log.info(f"### cleaned {clean_counter}/{len(keys)} aged redis keys ###")


def get_redis_key(r: redis.Redis, redis_key: str, age: float = 86400.0, prefix: str = "ckanext_isebel:") -> Any:
    """
    Return the value associated with the key from redis if it exists and not too old

    :param prefix:
    :param r: redis connection
    :param redis_key: key to get the value from
    :param age: age of the value in seconds
    :return:
    """
    if (r.exists(redis_key)
            and redis_key.split(":")[0] == prefix[:-1]
            and r.exists(f"{redis_key}_age")
            and convert_to_unix_timestamp(datetime.utcnow()) - float(r.get(f"{redis_key}_age")) < age):
        log.info(f"### getting {redis_key=} from redis ###")
        return json.loads(r.get(redis_key))
    else:
        return None


def set_redis_key(r: redis.Redis, redis_key: str, value: any) -> None:
    """
    Set the value associated with the key in redis

    :param r: redis connection
    :param redis_key: key to set the value to
    :param value: value to set
    :return:
    """
    r.set(redis_key, json.dumps(value))
    r.set(f"{redis_key}_age", convert_to_unix_timestamp(datetime.utcnow()))


def generate_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, HARD_LIMIT):
    full_results = get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, HARD_LIMIT)
    return get_map_result(full_results)


def make_redis_key(data_dict: dict, method: str = "md5", prefix: str = "ckanext_isebel:") -> str:
    """
    Make a redis key from the data_dict

    :param data_dict: data_dict to make the key from
    :param method: method to use to make the key
    :param prefix: prefix of the key
    :return:
    """
    if method == "md5":
        return f"{prefix}{hashlib.md5(json.dumps(data_dict).encode()).hexdigest()}"
    else:
        raise NotImplementedError(f"method {method} not implemented")


@bp.route("/dataset/", methods=["GET"])
def search(package_type: str = "dataset"):
    extra_vars: dict[str, Any] = {}

    try:
        context = cast(Context, {
            u'model': model,
            u'user': current_user.name,
            u'auth_user_obj': current_user
        })
        check_access(u'site_read', context)
    except NotAuthorized:
        base.abort(403, _(u'Not authorized to see this page'))

    # unicode format (decoded from utf8)
    extra_vars[u'q'] = q = request.args.get(u'q', u'')

    extra_vars['query_error'] = False
    page = h.get_page_number(request.args)

    limit = config.get(u'ckan.datasets_per_page')

    # most search operations should reset the page counter:
    params_nopage = [(k, v) for k, v in request.args.items(multi=True)
                     if k != u'page']

    extra_vars[u'remove_field'] = partial(remove_field, package_type)

    sort_by = request.args.get(u'sort', None)
    params_nosort = [(k, v) for k, v in params_nopage if k != u'sort']

    extra_vars[u'sort_by'] = partial(_sort_by, params_nosort, package_type)

    if not sort_by:
        sort_by_fields = []
    else:
        sort_by_fields = [field.split()[0] for field in sort_by.split(u',')]
    extra_vars[u'sort_by_fields'] = sort_by_fields

    pager_url = partial(_pager_url, params_nopage, package_type)

    details = _get_search_details()
    extra_vars[u'fields'] = details[u'fields']
    extra_vars[u'fields_grouped'] = details[u'fields_grouped']
    fq = details[u'fq']
    search_extras = details[u'search_extras']

    context = cast(Context, {
        u'model': model,
        u'session': model.Session,
        u'user': current_user.name,
        u'for_view': True,
        u'auth_user_obj': current_user
    })

    # Unless changed via config options, don't show other dataset
    # types any search page. Potential alternatives are do show them
    # on the default search page (dataset) or on one other search page
    search_all_type = config.get(u'ckan.search.show_all_types')
    search_all = False

    try:
        # If the "type" is set to True or False, convert to bool
        # and we know that no type was specified, so use traditional
        # behaviour of applying this only to dataset type
        search_all = asbool(search_all_type)
        search_all_type = u'dataset'
    # Otherwise we treat as a string representing a type
    except ValueError:
        search_all = True

    if not search_all or package_type != search_all_type:
        # Only show datasets of this particular type
        fq += u' +dataset_type:{type}'.format(type=package_type)

    facets: dict[str, str] = OrderedDict()

    org_label = h.humanize_entity_type(
        u'organization',
        h.default_group_type(u'organization'),
        u'facet label') or _(u'Organizations')

    group_label = h.humanize_entity_type(
        u'group',
        h.default_group_type(u'group'),
        u'facet label') or _(u'Groups')

    default_facet_titles = {
        u'organization': org_label,
        u'groups': group_label,
        u'tags': _(u'Tags'),
        u'res_format': _(u'Formats'),
        u'license_id': _(u'Licenses'),
    }

    for facet in h.facets():
        if facet in default_facet_titles:
            facets[facet] = default_facet_titles[facet]
        else:
            facets[facet] = facet

    # Facet titles
    for plugin in plugins.PluginImplementations(plugins.IFacets):
        facets = plugin.dataset_facets(facets, package_type)

    extra_vars[u'facet_titles'] = facets
    data_dict: dict[str, Any] = {
        u'q': q,
        u'fq': fq.strip(),
        u'facet.field': list(facets.keys()),
        u'rows': limit,
        u'start': (page - 1) * limit,
        u'sort': sort_by,
        u'extras': search_extras,
        u'include_private': config.get(
            u'ckan.search.default_include_private'),
    }
    map_results: list = []

    try:
        query = get_action(u'package_search')(context, data_dict)

        # loop the search query and get all the results
        # this workaround the 1000 rows solr hard limit
        HARD_LIMIT = 10000
        # conn = get_connection_redis()
        pager = 0
        # crank up the pager limit high as using points cluster for better performance
        PAGER_LIMIT = 10000

        data_dict_full_result = {
            'q': q,
            'fq': fq.strip(),
            'facet.field': list(facets.keys()),
            'rows': HARD_LIMIT,
            'start': 0,
            'sort': sort_by,
            'extras': search_extras,
            'include_private': config.get(
                u'ckan.search.default_include_private', True),
        }

        # adding map_results
        r = redis.connect_to_redis()
        redis_key: str = make_redis_key(data_dict, method="md5")

        delete_redis_keys(r, max_age=86400.0, limit=100)
        map_results = get_redis_key(r, redis_key)
        if map_results is None:
            log.info(f"### {redis_key=} not in redis ###")
            map_results = generate_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, HARD_LIMIT)
            set_redis_key(r, redis_key, map_results)

        extra_vars[u'sort_by_selected'] = query[u'sort']

        extra_vars[u'page'] = h.Page(
            collection=query[u'results'],
            page=page,
            url=pager_url,
            item_count=query[u'count'],
            items_per_page=limit
        )
        extra_vars[u'search_facets'] = query[u'search_facets']
        extra_vars[u'page'].items = query[u'results']
    except SearchQueryError as se:
        # User's search parameters are invalid, in such a way that is not
        # achievable with the web interface, so return a proper error to
        # discourage spiders which are the main cause of this.
        log.info(u'Dataset search query rejected: %r', se.args)
        base.abort(
            400,
            _(u'Invalid search query: {error_message}')
            .format(error_message=str(se))
        )
    except SearchError as se:
        # May be bad input from the user, but may also be more serious like
        # bad code causing a SOLR syntax error, or a problem connecting to
        # SOLR
        log.error(u'Dataset search error: %r', se.args)
        extra_vars[u'query_error'] = True
        extra_vars[u'search_facets'] = {}
        extra_vars[u'page'] = h.Page(collection=[])

    # FIXME: try to avoid using global variables
    g.search_facets_limits = {}
    default_limit: int = config.get(u'search.facets.default')
    for facet in cast(Iterable[str], extra_vars[u'search_facets'].keys()):
        try:
            limit = int(
                request.args.get(
                    u'_%s_limit' % facet,
                    default_limit
                )
            )
        except ValueError:
            base.abort(
                400,
                _(u'Parameter u"{parameter_name}" is not '
                  u'an integer').format(parameter_name=u'_%s_limit' % facet)
            )

        g.search_facets_limits[facet] = limit

    _setup_template_variables(context, {}, package_type=package_type)

    extra_vars[u'dataset_type'] = package_type

    # TODO: remove
    for key, value in extra_vars.items():
        setattr(g, key, value)

    extra_vars[u"map_results"] = map_results
    return base.render(
        _get_pkg_template(u'search_template', package_type), extra_vars
    )
