from ckan.lib import helpers as h
import ckan
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import logging
import json
import urllib.request as urllib2
from pprint import pprint
import requests

log = logging.getLogger(__name__)

def facet_get_extra_data_field(extras, field, lang=False):
    if not extras:
        return None

    extras_list = extras
    if extras_list and isinstance(extras_list, list):
        if lang:
            for item_dict in extras_list:
                k = item_dict.get('key').encode('utf-8')
                v = item_dict.get('value').encode('utf-8')
                if field in k:
                    return k, v
        else:
            for item_dict in extras_list:
                k = item_dict.get('key').encode('utf-8')
                v = item_dict.get('value').encode('utf-8')
                if k == field:
                    return k, v
    return None

def facet_get_similar_fields_from_extras(extras, field):
    result = list()
    if not extras:
        return None

    extras_list = extras
    if extras_list and isinstance(extras_list, list):
        for item_dict in extras_list:
            k = item_dict.get('key').encode('utf-8')
            v = item_dict.get('value').encode('utf-8')
            if field in k:
                result.append((k, v))
        return result
    return None

def facet_build_nav_main(*args):
    ''' build a set of menu items.

    args: tuples of (menu type, title) eg ('login', _('Login'))
    outputs <li><a href="...">title</a></li>
    '''
    output = ''
    for item in args:
        menu_item, title = item[:2]
        if len(item) == 3 and not h.check_access(item[2]):
            continue
        menu_item = h.map_pylons_to_flask_route_name(menu_item)
        output += h._make_menu_item(menu_item, title, class_='hcIsNav')
    return output


def facet_loadjson(orgstr, swap=True):
    '''return json obj'''
    json_data = json.loads(orgstr)
    if swap:
        if json_data['type'] == 'Point':
            json_data['coordinates'] = [[json_data['coordinates'][1], json_data['coordinates'][0]]]
        else:
            json_data['coordinates'] = map(lambda x: list(reversed(x)), reversed(json_data['coordinates']))
    return json_data

def facet_dumpjson(orgstr):
    return json.dumps(orgstr)

# def facet_nl2br(orgstr):


def facet_apisearch(q='', rows=99999):
    request = urllib2.Request(
        'http://ckan:5000/api/3/action/package_search?q=%s&rows=%s' % (q, rows))

    # Creating a dataset requires an authorization header.
    # request.add_header('Authorization', apikey)

    # Make the HTTP request.
    response = urllib2.urlopen(request)
    assert response.code == 200

    # Use the json module to load CKAN's response into a dictionary.
    response_dict = json.loads(response.read())
    assert response_dict['success'] is True
    return response_dict['result']


def facet_pprint(obj):
    pprint(obj)


def facet_len(obj):
        return len(obj) if obj else 0


def facet_vars(obj):
    return vars(obj)


def facet_capitalize(string):
    return string.capatalize


def facet_orgcount(org_id):
    request = urllib2.Request(
        'http://ckan:5000/api/3/action/organization_show?id=%s' % org_id )
    response = urllib2.urlopen(request)
    assert response.code == 200

    response_dict = json.loads(response.read())
    if response_dict['success'] is True:
        log.warning('%s has %s datasets' % (org_id, response_dict['result']['package_count']))
        if response_dict['result']['package_count'] > 40000:
            return 1
        else:
            return 2
        return response_dict['result']['package_count']
    else:
        return 0


def no_registering(context, data_dict):
    return {
        'success': False,
        'msg': toolkit._('Registration disabled. ')
    }


class IsebelPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IFacets, inherit=True)
    plugins.implements(plugins.IPackageController, inherit=True)
    plugins.implements(plugins.IAuthFunctions, inherit=True)
    plugins.implements(plugins.IConfigurer)
    # plugins.implements(plugins.IConfigurable)
    # plugins.implements(plugins.IBlueprint)
    # plugins.implements(plugins.IRoutes)
    # plugins.implements(plugins.ITranslation)
    plugins.implements(plugins.ITemplateHelpers)

    def get_auth_functions(self):
        return {
            'user_create': no_registering
        }

    def dataset_facets(self, facets_dict, package_type):
        return self._facets(facets_dict)

    def organization_facets(self, facets_dict, organization_type, package_type):
        return self._facets(facets_dict)

    def _facets(self, facets_dict):
        # Deleted facets
        if 'license_id' in facets_dict:
            del facets_dict['license_id']
        if 'res_format' in facets_dict:
            del facets_dict['res_format']
        if 'tags' in facets_dict:
            del facets_dict['tags']
        # Renamed facets
        if 'groups' in facets_dict:
            del facets_dict['groups']
            # facets_dict['groups'] = 'Communities'
            # del facets_dict['Communities']

        if 'notes' in facets_dict:
            facets_dict['notes'] = toolkit._('Notes')

        # New facets
        # facets_dict['identifier'] = toolkit._('Identifier')
        # facets_dict['index'] = toolkit._('Index')

        facets_dict['keyword'] = toolkit._('Keywords')
        facets_dict['da_keyword'] = toolkit._('Danish Keywords')
        facets_dict['nl_keyword'] = toolkit._('Dutch Keywords')
        facets_dict['deu_keyword'] = toolkit._('German Keywords')
        facets_dict['is_keyword'] = toolkit._('Icelandic Keywords')

        facets_dict['placeMentioned'] = toolkit._('Place Mentioned')
        facets_dict['place_narration'] = toolkit._('Place of Narration')
        facets_dict['person_narrator_gender'] = toolkit._('Narrator Gender')

        # facets_dict['machine_translation_target'] = toolkit._('Translation in English')

        # pprint(facets_dict)
        return facets_dict

    def update_config(self, config):
        toolkit.add_template_directory(config, "templates")
        toolkit.add_public_directory(config, "public")
        toolkit.add_resource("assets", "isebel")
        ckan.homepage_style = '4'

    def get_helpers(self):
        '''Register the most_popular_groups() function above as a template
        helper function.

        '''
        # Template helper function names should begin with the name of the
        # extension they belong to, to avoid clashing with functions from
        # other extensions.
        return {'facet_loadjson': facet_loadjson, 'facet_apisearch': facet_apisearch,
                'facet_pprint': facet_pprint, 'facet_len': facet_len, 'facet_vars': facet_vars,
                'facet_capitalize': facet_capitalize, 'facet_orgcount': facet_orgcount,
                'facet_build_nav_main': facet_build_nav_main, 'facet_dumpjson': facet_dumpjson,
                'facet_get_extra_data_field': facet_get_extra_data_field,
                'facet_get_similar_fields_from_extras': facet_get_similar_fields_from_extras
                }

    def before_map(self, map):
        """This IRoutes implementation overrides the standard
        ``/user/register`` behaviour with a custom controller.  You
        might instead use it to provide a completely new page, for
        example.
        Note that we have also provided a custom register form
        template at ``theme/templates/user/register.html``.
        """
        # Hook in our custom user controller at the points of creation
        # and edition.

        map.connect('/dataset',
                    controller='ckanext.facet.controller:CustomPakcageController',
                    action='search')
        # map.connect('/user/edit',
        #             controller='ckanext.example.controller:CustomUserController',
        #             action='edit')
        # map.connect('/user/edit/{id:.*}',
        #             controller='ckanext.example.controller:CustomUserController',
        #             action='edit')

        # map.connect('/package/new', controller='package_formalchemy', action='new')
        # map.connect('/package/edit/{id}', controller='package_formalchemy', action='edit')
        return map
