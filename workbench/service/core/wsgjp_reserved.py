"""Reserved LAN integration addresses; deliberately no authorization or delivery ACK.

Do not turn these into active endpoints with a configuration flag. A real adapter
must implement the vendor's authorization state, signature and durable receipt
contract first. Never parse, echo, persist or log credentials sent to placeholders.
"""
from flask import Blueprint, jsonify, make_response, render_template, request


def reserved_routes():
    routes = Blueprint('wsgjp_reserved', __name__, url_prefix='/integrations/wsgjp')

    def disabled(kind):
        if request.method in {'GET', 'HEAD'}:
            response = make_response(render_template('wsgjp_reserved.html', kind=kind), 503)
        else:
            response = make_response(jsonify(
                ok=False, enabled=False, error='integration_not_enabled',
                message='管家婆对接地址仅预留，尚未启用；未处理授权或业务通知。'), 503)
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-UL-Integration-State'] = 'reserved-disabled'
        return response

    @routes.route('/callback', methods=['GET', 'HEAD', 'POST', 'OPTIONS'], provide_automatic_options=False)
    def callback():
        return disabled('授权回调')

    @routes.route('/notify', methods=['GET', 'HEAD', 'POST', 'OPTIONS'], provide_automatic_options=False)
    def notify():
        return disabled('消息推送')

    return routes
