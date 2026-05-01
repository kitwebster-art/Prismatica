#!/usr/bin/env python3
"""Tiny static server for the Prismatica app.

allow_reuse_address ensures the watchdog can rebind immediately after a crash
or kill instead of waiting out the OS's TIME_WAIT window. Without it we hit
"OSError: Address already in use" on quick restarts.
"""
import http.server
import os
import socketserver
import sys

PORT = 8899
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIRECTORY)


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)


with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"Serving {DIRECTORY} at http://127.0.0.1:{PORT}")
    sys.stdout.flush()
    httpd.serve_forever()
