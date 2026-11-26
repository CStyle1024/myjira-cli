import io
import json
from contextlib import redirect_stdout, redirect_stderr


class Response(io.BytesIO):
    def __init__(self, body=b'', content_type='application/json', url='https://jira.test/rest/api/2/myself', status=200, headers=None):
        super().__init__(body)
        self.headers = {'Content-Type': content_type, **(headers or {})}
        self.url = url
        self.status = status

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def invoke(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def response_json(data):
    return Response(json.dumps(data).encode())


class ScriptedClient:
    base_url = 'https://jira.test'

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, path, query=None, body=None):
        self.calls.append((method, path, query, body))
        return self.handler(method, path, query or {}, body)
