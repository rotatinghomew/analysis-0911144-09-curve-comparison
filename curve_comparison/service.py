import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .analysis import compare
from .common import require
from .domain import records, summarize
from .repository import Conflict

_QUERY_STRATEGIES = {'none', 'cross_correlation', 'reference_shift'}


def _parse_float(query, name):
    require(name in query and len(query[name]) == 1, f'{name} 参数缺失或重复')
    try:
        return float(query[name][0])
    except ValueError as error:
        raise ValueError(f'{name} 必须是数值') from error


def _parse_int(query, name):
    require(name in query and len(query[name]) == 1, f'{name} 参数缺失或重复')
    try:
        value = int(query[name][0])
    except ValueError as error:
        raise ValueError(f'{name} 必须是整数') from error
    return value


def _comparison_params(query):
    strategy = query['strategy'][0] if 'strategy' in query else 'cross_correlation'
    require(strategy in _QUERY_STRATEGIES, f'strategy 只支持 {sorted(_QUERY_STRATEGIES)}')
    params = {
        'strategy': strategy,
        'grid_points': _parse_int(query, 'grid_points') if 'grid_points' in query else 51,
        'cap': _parse_float(query, 'cap') if 'cap' in query else 0.10,
    }
    if 'baseline' in query:
        require(len(query['baseline']) == 1, 'baseline 参数重复')
        params['baseline'] = query['baseline'][0]
    if strategy == 'reference_shift':
        require(len(query.get('shift', [])) == 2, 'reference_shift 需要两个 shift=treatment:value 参数')
        reference_shift = {}
        for item in query['shift']:
            label, separator, value = item.partition(':')
            require(separator and label, 'shift 参数格式应为 treatment:value')
            try:
                reference_shift[label] = float(value)
            except ValueError as error:
                raise ValueError('shift 数值不合法') from error
        params['reference_shift'] = reference_shift
    return params


def make_server(repository, host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == '/health':
                    return self.send_json(200, {'status': 'ok'})
                if parsed.path == '/revisions':
                    return self.send_json(200, {'revisions': repository.history()})
                if parsed.path not in ('/records', '/summary', '/comparison'):
                    return self.send_json(404, {'error': '接口不存在'})
                query = parse_qs(parsed.query)
                try:
                    revision = int(query['revision'][0]) if 'revision' in query else None
                except ValueError as error:
                    raise ValueError('revision 必须是整数') from error
                snapshot = repository.get(revision)
                if parsed.path == '/records':
                    result = records(snapshot['data'])
                elif parsed.path == '/summary':
                    result = summarize(snapshot['data'])
                else:
                    result = compare(snapshot['data'], **_comparison_params(query))
                self.send_json(200, {'revision': snapshot['revision'], 'digest': snapshot['digest'], 'result': result})
            except LookupError as error:
                self.send_json(404, {'error': str(error)})
            except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                self.send_json(400, {'error': str(error)})

        def do_POST(self):
            if urlsplit(self.path).path != '/imports':
                return self.send_json(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4_000_000:
                    raise ValueError('请求正文大小不合法')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求必须是对象')
                result = repository.import_data(payload['data'], payload['request_key'])
                self.send_json(200 if result['replayed'] else 201, result)
            except Conflict as error:
                self.send_json(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                self.send_json(400, {'error': str(error)})

    return ThreadingHTTPServer((host, port), Handler)
