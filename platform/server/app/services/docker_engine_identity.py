"""Read the local Windows Docker engine ID without loading Docker CLI plugins."""
import http.client
import json
import re
import sys


class Pipe:
    def __init__(self, channel):
        self.channel = channel

    def makefile(self, *_args):
        return self.channel


def query(name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name):
        raise ValueError('Invalid local Docker pipe name')
    with open('\\\\.\\pipe\\' + name, 'r+b', buffering=0) as channel:
        channel.write(b'GET /info HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n')
        response = http.client.HTTPResponse(Pipe(channel))
        response.begin()
        data = response.read(256 * 1024 + 1)
        if response.status != 200 or len(data) > 256 * 1024:
            raise ValueError('Docker engine identity is unavailable')
        identity = json.loads(data).get('ID')
        if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,120}', identity):
            raise ValueError('Docker engine did not return a valid ID')
        return identity


if __name__ == '__main__':
    print(query(sys.argv[1]))
