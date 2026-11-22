"""Private supervised transport; stdin/stdout protocol, no credentials on argv."""
import json
import sys
import urllib.request

from .oneapi import NoRedirect, OneAPIClient, OneAPIRequestError


def main():
    request = json.load(sys.stdin)
    client = OneAPIClient(urlopen=urllib.request.build_opener(NoRedirect()).open)
    try:
        data = client._request(request['route'], request['payload'], request['timeout'])
        json.dump({'data': data}, sys.stdout)
    except OneAPIRequestError as exc:
        json.dump({'error': exc.public()}, sys.stdout)


if __name__ == '__main__':
    main()
