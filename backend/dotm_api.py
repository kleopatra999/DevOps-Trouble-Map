#!/usr/bin/env python
# vim: ts=4 sw=4
# -*- coding: utf-8 -*-

from bottle import route, run, response, request, debug, static_file
from subprocess import check_output

# Backend Web API local imports
# FIXME: import only what is needed instead of *
from dotm_settings import *
from dotm_common import *
from dotm_queue import QResponse


# JSON Response helper functions
def json_error(message="Not Found", status_code=404):
    return '{"error": {"message": "' + message + '", "status_code": ' + str(status_code) + '}}'


def resp_json(resp=None):
    response.content_type = 'application/json'
    if not resp or resp == '[]':
        response.status = 404
        return json_error()
    return resp


def resp_jsonp(resp=None):
    response.content_type = 'application/javascript'
    callback = request.query.get('callback')
    if resp and callback:
        return '{}({})'.format(callback, resp)
    elif callback:
        return '{}({})'.format(callback, json_error())
    response.content_type = 'application/json'
    response.status = 400
    return json_error("No callback function provided")


def resp_or_404(resp=None, resp_type='application/json', cache_control='max-age=30, must-revalidate'):
    response.set_header('Cache-Control', cache_control)
    accepted_resp = ('application/json', 'application/javascript')
    resp_type_arr = request.headers.get('Accept').split(',')
    if resp_type_arr:
        for resp_type in resp_type_arr:
            if resp_type in accepted_resp:
                break
    if resp_type == 'application/javascript':
        return resp_jsonp(resp)
    return resp_json(resp)


# Redis helper functions
def history_call(f):
    """History decorator"""
    def wrap(*args, **kwargs):
        f.func_globals['ns'] = DOTMNamespace(request.query.get('history'))
        return f(*args, **kwargs)
    return wrap


# Backend Queue helper functions
def queue_func(fn, *args, **kwargs):
    rkey = '{}::result::{}'.format(ns.queue, str(uuid4()))
    qresp = QResponse(rdb, rkey, logger=None)
    qresp.queue(fn, args, kwargs)
    qresp.pending()
    return rkey


# Bottle HTTP routing
@route('/services')
@history_call
def get_services():
    services = {}
    monitoring = []
    connections = []
    nodes = rdb.lrange(ns.nodes, 0, -1)
    for node in nodes:
        monitoringDetails = get_node_alerts(node)
        if monitoringDetails and 'services_alerts' in monitoringDetails:
            for m in monitoringDetails['services_alerts']:
                monitoring.append({'service': m, 
                                   'status': monitoringDetails['services_alerts'][m],
                                   'node': node})

        serviceDetails = get_service_details(node)
        for s in serviceDetails:
            if not 'process' in serviceDetails[s]:
                name = 'port %s' % s
            else:
                name = serviceDetails[s]['process']
            if not name in services:
                services[name] = {}
                services[name]['nodes'] = []
            services[name]['nodes'].append(node)

    return resp_or_404(json.dumps({'services': services,
                                   'connections': connections,
                                   'monitoring': monitoring}),
				       'application/javascript',
					   'no-cache, no-store, must-revalidate')

@route('/geo/nodes')
@history_call
def get_geo_nodes():
    prefix = ns.resolver + '::ip_to_node::'
    ips = rdb.keys(prefix + '*')
    nodes = rdb.mget(ips)
    ips = [ip.replace(prefix, '') for ip in ips]
    geo = []
    for i, ip in enumerate(ips):
        try:
            result = gi.record_by_addr(ip)
            geo.append({
                'data': {
                    'node': nodes[i],
                    'monitoring': get_node_alerts(nodes[i]),
                    'ip': ip},
                'lat': result['latitude'],
                'lng': result['longitude']})
        except:
            pass

    return resp_or_404(json.dumps({'locations': geo}))


@route('/backend/nodes', method='GET')
@route('/nodes', method='GET')
@history_call
def get_nodes():
    monitoring = {}
    nodes = rdb.lrange(ns.nodes, 0, -1)
    for node in nodes:
        monitoring[node] = get_node_alerts(node)
    return resp_or_404(json.dumps({'nodes': list(monitoring.keys()),
                                   'monitoring': monitoring,
                                   'connections': get_connections()}),
				       'application/javascript',
					   'no-cache, no-store, must-revalidate')


@route('/backend/nodes', method='POST')
@route('/nodes', method='POST')
def add_or_remove_node():
    # FIXME: validate name
    action = request.forms.get('action')
    if action == "add":
        rdb.lpush(ns.nodes, request.forms.get('name'))
    elif action == "remove":
        rdb.lrem(ns.nodes, request.forms.get('name'), 1)


@route('/nodes/suggestions')
def node_suggestions():
    known_nodes = rdb.lrange(ns.nodes, 0, -1)
    suggested_nodes = []
    for line in check_output(['getent', 'hosts']).splitlines():
        print line
        fields = line.split()
        # FIXME: Maybe improve following matching to private AND known networks
        # FIXME: Poor mans grepping instead of correct network matching
        if re.match('^(10\.|172\.|192\.168\.)', fields[0]):
            # FIXME: checking if IP is resolved in dotm::resolver would be much 
            # much more exact. For now check if we know any alias in this line
            already_known = 0
            for name in fields:
                if name in known_nodes:
                    already_known = 1
            
            if already_known == 0:
                suggested_nodes.append(fields[1])

    return resp_or_404(json.dumps({'nodes': suggested_nodes}), 'application/javascript', 'no-cache, no-store, must-revalidate')


@route('/nodes/<name>', method='GET')
@history_call
def get_node(name):
    time_now = int(time.time())
    connection_aging = int(get_setting('aging')['Connections'])
    prefix = ns.nodes + '::' + name
    nodeDetails = rdb.hgetall(prefix)
    serviceDetails = get_service_details(name)

    # Fetch all connection details and expand known services
    # with their name and state details
    prefix = ns.connections + '::' + name + '::'
    connectionDetails = {}
    connections = [c.replace(prefix, '') for c in rdb.keys(prefix + '*')]
    for c in connections:
        cHash = rdb.hgetall(prefix + c)

        # Add connection freshness
        cHash['age'] = 'old'
        if 'last_seen' in cHash:
            if time_now - int(cHash['last_seen']) < connection_aging:
                cHash['age'] = 'fresh'

        # If remote host name is not an IP and port is not a high port
        # try to resolve service info
        try:
            if cHash['remote_port'] != 'high' and cHash['remote_host'] not in ('Internet', '127.0.0.1'):
                cHash['remote_service_id'] = '{}::{}::{}'.format(ns.services,
                                                                 cHash['remote_host'],
                                                                 cHash['remote_port'])
                cHash['remote_service'] = rdb.hgetall(cHash['remote_service_id'])

        except KeyError:
            print "Bad: key missing, could be a migration issue..."
        connectionDetails[c] = cHash

    serviceAlerts = []
    for s in rdb.lrange(ns.services_checks + '::' + name, 0, -1):
        serviceAlerts.append(dict(json.loads(s)))

    return resp_or_404(json.dumps({'name': name,
                                   'status': nodeDetails,
                                   'services': serviceDetails,
                                   'connections': connectionDetails,
                                   'monitoring': {
                                       'node': get_node_alerts(name),
                                       'services': serviceAlerts}}))


@route('/backend/settings/<action>/<key>', method='POST')
@route('/settings/<action>/<key>', method='POST')
# NOTE: imho ideologically incorrect API interface. My suggestion would be to
# implement API as /settings/key, when it is needed make use of
# /settings/key&type=hash. As HTML5 at the moment is limited to forms methods
# ["GET"|"POST"] we can add /settings/key&type=hash&action=<action>, but at the
# same time support HTTP methods ["GET"|"POST"|"PUT"|"DELETE"] for actions.
def change_settings(action, key):
    if key in settings:
        if action == 'set' and settings[key]['type'] == 'simple_value':
                rdb.set(ns.config + '::' + key, request.forms.get('value'))
        elif action == 'add' and settings[key]['type'] == 'array':
                rdb.lpush(ns.config + '::' + key, request.forms.get('value'))
        elif action == 'remove' and settings[key]['type'] == 'array':
                rdb.lrem(ns.config + '::' + key, request.forms.get('key'), 1)
        elif action == 'setHash' and settings[key]['type'] == 'hash':
                # setHash might set multiple enumerated keys, e.g. to set all
                # Nagios instance settings, therefore we need to loop here
                i = 1
                while request.forms.get('key' + str(i)):
                    rdb.hset(ns.config + '::' + key,
                             request.forms.get('key' + str(i)),
                             request.forms.get('value' + str(i)))
                    i += 1
        elif action == 'delHash' and settings[key]['type'] == 'hash':
                rdb.hdel(ns.config + '::' + key, request.forms.get('key'))
        else:
            return json_error("This is not a valid command and settings type combination", 400)
        return "OK"
    else:
        return json_error("This is not a valid settings key or settings command", 400)


@route('/backend/settings', method='GET')
@route('/settings', method='GET')
@history_call
def get_settings():
    for s in settings:
        settings[s]['values'] = get_setting(s)
    return resp_or_404(json.dumps(settings), 'application/javascript', 'no-cache, no-store, must-revalidate')


@route('/mon/nodes')
@history_call
def get_mon_nodes():
    node_arr = rdb.keys(ns.nodes_checks + '::*')
    return resp_or_404(json.dumps([n.split('::')[-1] for n in node_arr])
                       if node_arr else None)


@route('/mon/nodes/<node>')
@history_call
def get_mon_node(node):
    return resp_or_404(rdb.get(ns.nodes_checks + '::' + node))


@route('/mon/services/<node>')
@history_call
def get_mon_node_services(node):
    return resp_or_404(json.dumps(get_json_array(ns.services_checks + '::' + node)))


@route('/mon/nodes/<node>/<key>')
@history_call
def get_mon_node_key(node, key):
    result = None
    node_str = rdb.get(ns.nodes_checks + '::' + node)
    if node_str:
        node_obj = json.loads(node_str)
        if key in node_obj:
            result = vars_to_json(key, node_obj[key])
    return resp_or_404(result)


# FIXME: Ugly implementation just as POC, callback should be stored in a session.
# Unfortunately bottle-sessions is not included in to Ubuntu repo...
@route('/mon/reload', method='POST')
def mon_reload():
    response.status = 303
    response.set_header('Location', '/queue/result/' + queue_func('reload'))
    return


@route('/queue/result/'
       '<key:re:dotm::queue::result::[a-f0-9]{8}-?[a-f0-9]{4}-?4[a-f0-9]{3}-?[89ab][a-f0-9]{3}-?[a-f0-9]{12}\Z>',
       method=['GET', 'POST'])
def queue_result(key):
    return resp_or_404(rdb.get(key))


@route('/history', method='GET')
def get_history():
    return resp_or_404(json.dumps(rdb.lrange(ns.history, 0, -1)))


@route('/config', method='GET')
@history_call
def get_config():
    return resp_or_404(json.dumps(rdb.hgetall(ns.config)))


@route('/config/<variable>', method='GET')
@history_call
def get_config_variable(variable):
    value = rdb.hget(ns.config, variable)
    if value:
        return resp_or_404(vars_to_json(variable, value))
    return resp_or_404()


@route('/report', method='GET')
def get_report():
    """ Returns data for a system report including information about
            - unmonitored services
            - unused services
    """
    # FIXME: Do calculation in backend (better for Nagios checks too)
    # and just return results here.
    alerts = {}
    nodes = rdb.lrange(ns.nodes, 0, -1)
    for node in nodes:
        alerts[node] = []
        monitoring = get_node_alerts(node)
        nodeDetails = rdb.hgetall(ns.nodes + '::' + node)
        serviceDetails = get_service_details(node)
        if not monitoring:
            alerts[node].append({'category': 'monitoring',
                                 'severity': 'WARNING',
                                 'message': 'Monitoring missing for this node'})

        if 'fetch_status' in nodeDetails and nodeDetails['fetch_status'] != 'OK':
            alerts[node].append({'category': 'agent',
                                 'severity': 'CRITICAL',
                                 'message': nodeDetails['fetch_status']})
		# FIXME: check on out-dated results too!
        elif not nodeDetails:
            alerts[node].append({'category': 'agent',
                                 'severity': 'WARNING',
                                 'message': 'No info for this node fetched from remote agent.'})

        for s in serviceDetails:
            # If there is Nagios alert data and some service checks could not be mapped to services...
            if monitoring and not 'alert_status' in serviceDetails[s]:
                alerts[node].append({'category': 'monitoring',
                                     'severity': 'WARNING',
                                     'message': 'Service "'
                                                + serviceDetails[s]['process']
                                                + '" has no known service check.'})
            if 'age' in serviceDetails and serviceDetails[s]['age'] == 'old':
                alerts[node].append({'category': 'usage',
                                     'severity': 'WARNING',
                                     'message': 'Service "'
                                                + serviceDetails[s]['process']
                                                + '" is unused for quite some time.'})

    return resp_or_404(json.dumps({'nodes': nodes,
                                   'alerts': alerts}))


@route('/config', method='POST')
def set_config():
    try:
        data_obj = json.loads(request.body.readlines()[0])
        if not isinstance(data_obj, dict):
            raise ValueError
        if not data_obj.viewkeys():
            raise ValueError
    except (ValueError, IndexError):
        response.status = 400
        return json_error("Wrong POST data format", 400)

    for key, val in data_obj.items():
        # TODO: allow only defined variable names with defined value type and
        # maximum length
        rdb.hset(ns.config, key, val)
    return resp_or_404(json.dumps(data_obj))


# Serve static content to eliminate the need of apache in development env.
# For this to work additional routes for /backend/* paths were added because
# they are used in the frontend.
@route('/')
@route('/<filename:path>')
def static(filename="index.htm"):
    return static_file(filename, "../frontend/static/")


if __name__ == '__main__':
    debug(mode=True)
    run(host='localhost', port=8080, reloader=True)
