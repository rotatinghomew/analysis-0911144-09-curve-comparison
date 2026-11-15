import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Client:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip('/')

    def request(self, path, body=None):
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request(self.base_url + path, data=encoded, headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def import_data(self, data, request_key):
        return self.request('/imports', {'data': data, 'request_key': request_key})

    def summary(self, revision=None):
        return self.request('/summary' + ('' if revision is None else f'?revision={revision}'))

    def records(self, revision=None):
        return self.request('/records' + ('' if revision is None else f'?revision={revision}'))

    def comparison(self, revision=None, strategy='cross_correlation', cap=0.10,
                   grid_points=51, baseline=None, reference_shift=None):
        query = {'strategy': strategy, 'cap': cap, 'grid_points': grid_points}
        if revision is not None:
            query['revision'] = revision
        if baseline is not None:
            query['baseline'] = baseline
        if reference_shift is not None:
            query['shift'] = [f'{label}:{value}' for label, value in reference_shift.items()]
        return self.request('/comparison?' + urlencode(query, doseq=True))
