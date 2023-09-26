# encoding: utf-8

import logging
import urllib2
from pprint import pprint
from urllib import urlencode
import datetime
import mimetypes
import cgi

from ckan.common import config
from paste.deploy.converters import asbool
import paste.fileapp

import ckan.logic as logic
import ckan.lib.base as base
import ckan.lib.i18n as i18n
import ckan.lib.maintain as maintain
import ckan.lib.navl.dictization_functions as dict_fns
import ckan.lib.helpers as h
import ckan.model as model
import ckan.lib.datapreview as datapreview
import ckan.lib.plugins
import ckan.lib.uploader as uploader
import ckan.plugins as p
import ckan.lib.render

from ckan.common import OrderedDict, _, json, request, c, response, g

from ckan.controllers.package import PackageController

log = logging.getLogger(__name__)

render = base.render
abort = base.abort

NotFound = logic.NotFound
NotAuthorized = logic.NotAuthorized
ValidationError = logic.ValidationError
check_access = logic.check_access
get_action = logic.get_action
tuplize_dict = logic.tuplize_dict
clean_dict = logic.clean_dict
parse_params = logic.parse_params
flatten_to_string_key = logic.flatten_to_string_key

lookup_package_plugin = ckan.lib.plugins.lookup_package_plugin

HOSTNAME = 'redis'
# REDIS_PORT = 6379
# REDIS_DB = 0

# settings for AMQP
EXCHANGE_TYPE = 'direct'
EXCHANGE_NAME = 'ckan.harvest'


def get_connection_redis():
    log.info('### getting redis connection from host [%s]' % HOSTNAME)
    try:
        import redis
    except Exception as e:
        log.error('Cannot import redis; Error is {}'.format(e.message))
    try:
        return redis.Redis(host=HOSTNAME)
    except Exception as e:
        log.error('Cannot get redis connection to host {}; error is {}'.format(HOSTNAME, e.message))


def _encode_params(params):
    return [(k, v.encode('utf-8') if isinstance(v, string_types) else str(v))
            for k, v in params]


def url_with_params(url, params):
    params = _encode_params(params)
    return url + u'?' + urlencode(params)


def search_url(params, package_type=None):
    if not package_type or package_type == 'dataset':
        url = h.url_for(controller='package', action='search')
    else:
        url = h.url_for('{0}_search'.format(package_type))
    return url_with_params(url, params)


class CustomPakcageController(PackageController):

    def get_full_results(self, context, data_dict_full_result, pager, PAGER_LIMIT, q, fq, facets, HARD_LIMIT, sort_by,
                         search_extras):
        log.info('before loop: {}'.format(data_dict_full_result))
        query_full_result = get_action('package_search')(context, data_dict_full_result)
        full_results = list()
        while query_full_result.get('results', None) and pager < PAGER_LIMIT:
            full_results.extend(query_full_result.get('results', None))
            pager += 1
            # data_dict_full_result = {
            #     'q': q,
            #     'fq': fq.strip(),
            #     'facet.field': facets.keys(),
            #     'rows': HARD_LIMIT,
            #     'start': pager * HARD_LIMIT,
            #     'sort': sort_by,
            #     'extras': search_extras,
            #     'include_private': asbool(config.get(
            #         'ckan.search.default_include_private', True)),
            # }
            data_dict_full_result['start'] = pager * HARD_LIMIT
            # log.info('in loop: {}'.format(data_dict_full_result))
            query_full_result = get_action('package_search')(context, data_dict_full_result)
            # log.info('result: {}'.format(query_full_result.get('results', None)))
        # log.info('full results: {}'.format(full_results))
        return full_results

    def facet_loadjson(self, orgstr, swap=True):
        '''return json obj'''
        json_data = json.loads(orgstr)
        if swap:
            if json_data['type'] == 'Point':
                json_data['coordinates'] = [[json_data['coordinates'][1], json_data['coordinates'][0]]]
            else:
                json_data['coordinates'] = map(lambda x: list(reversed(x)), reversed(json_data['coordinates']))
        return json_data

    def get_map_result(self, full_results):
        map_results = list()
        for r in full_results:
            for urlDict in r['extras']:
                if urlDict['key'] == 'identifier':
                    current_url = urlDict['value']
            for spatial in r['extras']:
                if spatial['key'] == 'spatial':
                    markers = self.facet_loadjson(spatial['value'])
                    for geopoint in markers['coordinates']:
                        # map_results.append([geopoint[0], geopoint[1], r['name'], current_url])
                        map_results.append([geopoint[0], geopoint[1], r['name'], r['name']])
            # log.info('######################## {}'.format(r['name']))
        return map_results

    def get_all_packages(self, max_records=99999):
        request = urllib2.Request('http://ckan:5000/api/3/action/package_search?q=*:*&rows=%s' % max_records)

        response = urllib2.urlopen(request)

        if response:
            if response.code == 200:
                # Use the json module to load CKAN's response into a dictionary.
                response_dict = json.loads(response.read())
                assert response_dict['success'] is True

                return response_dict
        return None

    def search(self):
        from ckan.lib.search import SearchError, SearchQueryError

        package_type = self._guess_package_type()

        try:
            context = {'model': model, 'user': c.user,
                       'auth_user_obj': c.userobj}
            check_access('site_read', context)
        except NotAuthorized:
            abort(403, _('Not authorized to see this page'))

        # unicode format (decoded from utf8)
        q = c.q = request.params.get('q', u'')
        org = request.params.get('organization', None)
        c.query_error = False
        page = h.get_page_number(request.params)

        try:
            limit = int(config.get('ckan.datasets_per_page', 20))
        except:
            limit = 20

        # most search operations should reset the page counter:
        params_nopage = [(k, v) for k, v in request.params.items()
                         if k != 'page']

        def drill_down_url(alternative_url=None, **by):
            return h.add_url_param(alternative_url=alternative_url,
                                   controller='package', action='search',
                                   new_params=by)

        c.drill_down_url = drill_down_url

        def remove_field(key, value=None, replace=None):
            return h.remove_url_param(key, value=value, replace=replace,
                                      controller='package', action='search',
                                      alternative_url=package_type)

        c.remove_field = remove_field

        sort_by = request.params.get('sort', None)
        params_nosort = [(k, v) for k, v in params_nopage if k != 'sort']

        def _sort_by(fields):
            """
            Sort by the given list of fields.

            Each entry in the list is a 2-tuple: (fieldname, sort_order)

            eg - [('metadata_modified', 'desc'), ('name', 'asc')]

            If fields is empty, then the default ordering is used.
            """
            params = params_nosort[:]

            if fields:
                sort_string = ', '.join('%s %s' % f for f in fields)
                params.append(('sort', sort_string))
            return search_url(params, package_type)

        c.sort_by = _sort_by
        if not sort_by:
            c.sort_by_fields = []
        else:
            c.sort_by_fields = [field.split()[0]
                                for field in sort_by.split(',')]

        def pager_url(q=None, page=None):
            params = list(params_nopage)
            params.append(('page', page))
            return search_url(params, package_type)

        c.search_url_params = urlencode(_encode_params(params_nopage))

        try:
            c.fields = []
            # c.fields_grouped will contain a dict of params containing
            # a list of values eg {'tags':['tag1', 'tag2']}
            c.fields_grouped = {}
            search_extras = {}
            fq = ''
            for (param, value) in request.params.items():
                if param not in ['q', 'page', 'sort'] \
                        and len(value) and not param.startswith('_'):
                    if not param.startswith('ext_'):
                        c.fields.append((param, value))
                        fq += ' %s:"%s"' % (param, value)
                        if param not in c.fields_grouped:
                            c.fields_grouped[param] = [value]
                        else:
                            c.fields_grouped[param].append(value)
                    else:
                        search_extras[param] = value

            context = {'model': model, 'session': model.Session,
                       'user': c.user, 'for_view': True,
                       'auth_user_obj': c.userobj}

            # Unless changed via config options, don't show other dataset
            # types any search page. Potential alternatives are do show them
            # on the default search page (dataset) or on one other search page
            search_all_type = config.get(
                'ckan.search.show_all_types', 'dataset')
            search_all = False

            map_results = None
            try:
                # If the "type" is set to True or False, convert to bool
                # and we know that no type was specified, so use traditional
                # behaviour of applying this only to dataset type
                search_all = asbool(search_all_type)
                search_all_type = 'dataset'
            # Otherwise we treat as a string representing a type
            except ValueError:
                search_all = True

            if not package_type:
                package_type = 'dataset'

            if not search_all or package_type != search_all_type:
                # Only show datasets of this particular type
                fq += ' +dataset_type:{type}'.format(type=package_type)

            facets = OrderedDict()

            default_facet_titles = {
                'organization': _('Organizations'),
                'groups': _('Groups'),
                'tags': _('Tags'),
                'res_format': _('Formats'),
                'license_id': _('Licenses'),
            }

            for facet in h.facets():
                if facet in default_facet_titles:
                    facets[facet] = default_facet_titles[facet]
                else:
                    facets[facet] = facet

            # Facet titles
            for plugin in p.PluginImplementations(p.IFacets):
                facets = plugin.dataset_facets(facets, package_type)

            c.facet_titles = facets

            data_dict = {
                'q': q,
                'fq': fq.strip(),
                'facet.field': facets.keys(),
                'rows': limit,
                'start': (page - 1) * limit,
                'sort': sort_by,
                'extras': search_extras,
                'include_private': asbool(config.get(
                    'ckan.search.default_include_private', True)),
            }

            query = get_action('package_search')(context, data_dict)

            # loop the search query and get all the results
            # this workaround the 1000 rows solr hard limit
            HARD_LIMIT = 1000
            conn = get_connection_redis()
            pager = 0
            # crank up the pager limit high as using points cluster for better performance
            PAGER_LIMIT = 10000

            data_dict_full_result = {
                'q': q,
                'fq': fq.strip(),
                'facet.field': facets.keys(),
                'rows': HARD_LIMIT,
                'start': 0,
                'sort': sort_by,
                'extras': search_extras,
                'include_private': asbool(config.get(
                    'ckan.search.default_include_private', True)),
            }

            if not org:
                # if no q, it is an init load or direct visit on / dataset
                log.info('### Not org ###')
                if not q:
                    log.info('### Not q ###')
                    if not conn.exists('redis_full_results'):
                        log.info('### generating full results ###')
                        # get full results and add to redis when there is not full results in redis
                        full_results = self.get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, q,
                                                             fq, facets, HARD_LIMIT, sort_by, search_extras)
                        map_results = self.get_map_result(full_results)
                        # adding to redis
                        log.info('adding full results to redis')
                        conn.set('redis_full_results', json.dumps(map_results))
                        # log.info(c.full_results)
                    else:
                        log.info('### using cached full results ###')
                        map_results = json.loads(conn.get('redis_full_results'))
                        # log.info(c.full_results)
                else:
                    log.info('### With q ###')
                    full_results = self.get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, q,
                                                         fq, facets, HARD_LIMIT, sort_by, search_extras)
                    map_results = self.get_map_result(full_results)
            else:
                log.info('### With org ###')
                if not q:
                    log.info('### Not q ###')
                    if not conn.exists('redis_full_results_%s' % org):
                        log.info('### generating %s results ###' % org)
                        # get full results and add to redis when there is not full results in redis
                        full_results = self.get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, q,
                                                             fq, facets, HARD_LIMIT, sort_by, search_extras)
                        map_results = self.get_map_result(full_results)
                        # adding to redis
                        log.info('adding %s results to redis' % org)
                        conn.set('redis_full_results_%s' % org, json.dumps(map_results))
                        # log.info(c.full_results)
                    else:
                        log.info('### using cached %s results ###' % org)
                        map_results = json.loads(conn.get('redis_full_results_%s' % org))
                        # log.info(c.full_results)
                else:
                    log.info('### With q ###')
                    full_results = self.get_full_results(context, data_dict_full_result, pager, PAGER_LIMIT, q,
                                                         fq, facets, HARD_LIMIT, sort_by, search_extras)
                    map_results = self.get_map_result(full_results)
                # log.info(c.full_results)

            c.sort_by_selected = query['sort']

            c.page = h.Page(
                collection=query['results'],
                page=page,
                url=pager_url,
                item_count=query['count'],
                items_per_page=limit
            )
            c.search_facets = query['search_facets']
            c.page.items = query['results']
        except SearchQueryError as se:
            # User's search parameters are invalid, in such a way that is not
            # achievable with the web interface, so return a proper error to
            # discourage spiders which are the main cause of this.
            log.info('Dataset search query rejected: %r', se.args)
            abort(400, _('Invalid search query: {error_message}')
                  .format(error_message=str(se)))
        except SearchError as se:
            # May be bad input from the user, but may also be more serious like
            # bad code causing a SOLR syntax error, or a problem connecting to
            # SOLR
            log.error('Dataset search error: %r', se.args)
            c.query_error = True
            c.search_facets = {}
            c.page = h.Page(collection=[])
        except NotAuthorized:
            abort(403, _('Not authorized to see this page'))

        c.search_facets_limits = {}
        for facet in c.search_facets.keys():
            try:
                limit = int(request.params.get('_%s_limit' % facet,
                                               int(config.get('search.facets.default', 10))))
            except ValueError:
                abort(400, _('Parameter "{parameter_name}" is not '
                             'an integer').format(
                    parameter_name='_%s_limit' % facet))
            c.search_facets_limits[facet] = limit

        self._setup_template_variables(context, {},
                                       package_type=package_type)

        return render(self._search_template(package_type),
                      extra_vars={'dataset_type': package_type, 'map_results': map_results})
