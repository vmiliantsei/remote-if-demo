#!/usr/bin/env python3

"""
Remote-IF script. When run, this brings up a Tornado web server which
allows clients to play an IF game via RemGlk / GlkOte.

This supports both AJAX and websocket messages from GlkOte.

Written by Andrew Plotkin. This script is in the public domain.
"""

import logging
import os
import json
import binascii
import shlex

import tornado.web
import tornado.websocket
import tornado.gen
import tornado.ioloop
import tornado.options

from blorbtool import BlorbFile

tornado.options.define(
    'port', type=int, default=4000,
    help='port number to listen on')

tornado.options.define(
    'address', type=str, default='localhost',
    help='address to listen on')

tornado.options.define(
    'debug', type=bool,
    help='application debugging (see Tornado docs)')

tornado.options.define(
    'command', type=str,
    help='shell command to run a RemGlk game')

tornado.options.define(
    'game', type=str,
    help='Game file to run')

tornado.options.define(
    'connect', type=str, default='ajax',
    help='connection method: "ajax" or "ws"')

tornado.options.define(
    'gidebug', type=bool,
    help='activate the glkote debug console')

# Parse 'em up.
tornado.options.parse_command_line()
opts = tornado.options.options

if not opts.command:
    raise Exception('Must supply --command argument')

if opts.connect not in ('ajax', 'ws'):
    raise Exception('The --connect argument must be "ajax" or "ws"')

# Define application options which are always set.
appoptions = {
    'xsrf_cookies': True,
    'template_path': './templates',
    'static_path': './static',
    'cookie_secret': '__FILL_IN_RANDOM_DATA_HERE__',
    }

# Pull out some of the config-file options to pass along to the application.
for key in [ 'debug' ]:
    val = getattr(opts, key)
    if val is not None:
        appoptions[key] = val

if opts.game:
    blorbfile = BlorbFile(opts.game)
    args = [
        os.path.realpath(opts.command),
        '-ru', 'http://' + opts.address + ':' + str(opts.port) + '/resource/',
        opts.game,
    ]
    cwd = os.path.dirname(opts.game)
else:
    args = shlex.split(opts.command)
    cwd = None

class MainHandler(tornado.web.RequestHandler):
    # Handle the "/" URL: the login screen
    
    async def get(self):
        sessionid = self.get_secure_cookie('sessionid')
        self.render('main.html', sessionid=sessionid)
        
    async def post(self):
        if self.get_argument('signin', None):
            # Create a random sessionid string
            sessionid = binascii.hexlify(os.urandom(16)) # bytes, not string
            self.set_secure_cookie('sessionid', sessionid, expires_days=10)
        elif self.get_argument('signout', None):
            sessionid = None
            self.clear_cookie('sessionid')
        else:
            raise Exception('Unknown form button')
        self.render('main.html', sessionid=sessionid)
        
class PlayHandler(tornado.web.RequestHandler):
    # Handle the "/play" URL: the game screen, and AJAX messages from GlkOte
    
    def check_xsrf_cookie(self):
        # All the form input on this page is GlkOte AJAX requests,
        # so we'll skip XSRF checking. (If we wanted to include XSRF,
        # we could embed {{ xsrf_token }} in the play.html template.)
        pass
    
    async def get(self):
        sessionid = self.get_secure_cookie('sessionid')
        if not sessionid:
            raise Exception('You are not logged in')
        
        session = self.application.sessions.get(sessionid)
        if not session:
            session = Session(self.application, sessionid)
            self.application.sessions[sessionid] = session
            self.application.log.info('Created session object %s', session)
            
        self.render('play.html', connecttype=opts.connect, gidebug=opts.gidebug)
        
    async def post(self):
        #print('REQ', self.request.body.decode())
        sessionid = self.get_secure_cookie('sessionid')
        if not sessionid:
            raise Exception('You are not logged in')
        session = self.application.sessions.get(sessionid)
        if not session:
            raise Exception('No session found')

        # Start the game process if it's not already running.
        if not session.proc:
            session.launch()

        # This logic relies on the proper behavior of the RemGlk library:
        # that it produces exactly one JSON output for every JSON input.
        
        session.input(self.request.body)
        res = await session.gameread()
        #print('RES', res.decode())

        self.write(res)
        self.set_header("Content-Type", "application/json; charset=UTF-8")

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    # Handle websocket connections from GlkOte.

    def open(self):
        sessionid = self.get_secure_cookie('sessionid')
        if not sessionid:
            raise Exception('You are not logged in')
        
        session = self.application.sessions.get(sessionid)
        if not session:
            session = Session(self.application, sessionid)
            self.application.sessions[sessionid] = session
            self.application.log.info('Created session object %s', session)

        self.sessionid = sessionid

        # Start the game process.
        if not session.proc:
            session.launch()

        # Now we wait for the first message from GlkOte.

    async def on_message(self, msg):
        # Pass message from the websocket to the game session.
        session = self.application.sessions.get(self.sessionid)
        if not session:
            raise Exception('No session found')
        
        #print('REQ', msg)
        session.input(msg.encode('utf-8'))

        res = await session.gameread()
        #print('RES', res.decode())
        
        # Pass message from the game session to the websocket.
        self.write_message(res)

    def on_close(self):
        # Websocket is gone; kill the game session.
        # (It would be nicer to wait a few minutes to see if the
        # session comes back via a new websocket.)
        
        session = self.application.sessions.get(self.sessionid)
        if not session:
            raise Exception('No session found')
        
        self.application.log.info('Session %s has disconnected', session)
        session.close()
        del self.application.sessions[self.sessionid]

class ResourcesHandler(tornado.web.RequestHandler):
    # Handle the "/resource" URL

    async def get(self, *args, **kwargs):
        type = str.encode(args[0].capitalize())
        num = int(args[1])
        chunk = blorbfile.usagemap.get((type, num))
        self.write(chunk.data())
        # self.set_header("Content-Type", "image/" + str(chunk.type).lower())

class Session:
    """The Session class represents a logged-in player. The Session contains
    the link to the RemGlk/Glulxe subprocess.
    """
    
    def __init__(self, app, sessionid):
        self.log = app.log
        self.id = sessionid
        self.proc = None
        
    def __repr__(self):
        return '<Session "%s">' % (self.id.decode(),)

    def launch(self):
        """Start the interpreter subprocess.
        """
        self.log.info('Launching game for %s', self)
        
        self.proc = tornado.process.Subprocess(
            args,
            cwd=cwd,
            close_fds=True,
            stdin=tornado.process.Subprocess.STREAM,
            stdout=tornado.process.Subprocess.STREAM)

    def close(self):
        """Shut down the interpreter subprocess. We call this if the GlkOte
        library disconnects. (We can detect that for a websocket connection,
        but not for an AJAX connection.)
        """
        if not self.proc:
            return
        self.proc.stdin.close()
        self.proc = None

    def input(self, msg):
        """Pass an update (bytes) along to the game.
        """
        self.proc.stdin.write(msg)

    async def gameread(self):
        """Await the next game response.
        """
        if self.proc is None:
            # Closed, never mind.
            return None

        try:
            return await self.proc.stdout.read_until(b'\n\n')
        except tornado.iostream.StreamClosedError:
            return b''

    #def gameclosed(self, msg):
    #    """Callback for game process termination. (Technically, EOF on
    #    the game's stdout.)
    #    """
    #    self.log.info('Game has terminated!')
    
        
# Core handlers.
handlers = [
    (r'/', MainHandler),
    (r'/play', PlayHandler),
    (r'/websocket', WebSocketHandler),
    (r'/resource/(\w+)-(\d+)\.(\w+)', ResourcesHandler),
]

class MyApplication(tornado.web.Application):
    """MyApplication is a customization of the generic Tornado web app
    class.
    """
    def init_app(self):
        # Grab the same logger that tornado uses.
        self.log = logging.getLogger("tornado.general")

        # Session repository; maps session ID to session objects.
        self.sessions = {}

application = MyApplication(
    handlers,
    **appoptions)

# Boilerplate to launch the web server.
application.init_app()
application.listen(opts.port, opts.address)
tornado.ioloop.IOLoop.current().start()

