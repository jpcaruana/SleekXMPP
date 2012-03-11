import uuid
import logging
import threading

from sleekxmpp import Message, Iq
from sleekxmpp.exceptions import XMPPError, IqError, IqTimeout
from sleekxmpp.xmlstream.handler import Callback
from sleekxmpp.xmlstream.matcher import StanzaPath
from sleekxmpp.xmlstream import register_stanza_plugin
from sleekxmpp.plugins.base import base_plugin
from sleekxmpp.plugins.xep_0047 import stanza, Open, Close, Data, IBBytestream


log = logging.getLogger(__name__)


class xep_0047(base_plugin):

    def plugin_init(self):
        self.xep = '0047'
        self.description = 'In-Band Bytestreams'
        self.stanza = stanza

        self.streams = {}
        self.pending_streams = {3: 5}
        self.pending_close_streams = {}
        self._stream_lock = threading.Lock()

        self.max_block_size = self.config.get('max_block_size', 8192)
        self.window_size = self.config.get('window_size', 1)
        self.auto_accept = self.config.get('auto_accept', True)
        self.accept_stream = self.config.get('accept_stream', None)

        register_stanza_plugin(Iq, Open)
        register_stanza_plugin(Iq, Close)
        register_stanza_plugin(Iq, Data)

        self.xmpp.register_handler(Callback(
            'IBB Open',
            StanzaPath('iq@type=set/ibb_open'),
            self._handle_open_request))

        self.xmpp.register_handler(Callback(
            'IBB Close',
            StanzaPath('iq@type=set/ibb_close'),
            self._handle_close))

        self.xmpp.register_handler(Callback(
            'IBB Data',
            StanzaPath('iq@type=set/ibb_data'),
            self._handle_data))

    def post_init(self):
        self.xmpp['xep_0030'].add_feature('http://jabber.org/protocol/ibb')

    def _accept_stream(self, iq):
        if self.accept_stream is not None:
            return self.accept_stream(iq)
        if self.auto_accept:
            if iq['ibb_open']['block_size'] <= self.max_block_size:
                return True
        return False

    def open_stream(self, jid, block_size=4096, sid=None, window=1,
                    ifrom=None, block=True, timeout=None, callback=None):
        if sid is None:
            sid = str(uuid.uuid4())

        iq = self.xmpp.Iq()
        iq['type'] = 'set'
        iq['to'] = jid
        iq['from'] = ifrom
        iq['ibb_open']['block_size'] = block_size
        iq['ibb_open']['sid'] = sid
        iq['ibb_open']['stanza'] = 'iq'

        stream = IBBytestream(self.xmpp, sid, block_size,
                              iq['to'], iq['from'], window)

        with self._stream_lock:
            self.pending_streams[iq['id']] = stream

        self.pending_streams[iq['id']] = stream

        if block:
            resp = iq.send(timeout=timeout)
            self._handle_opened_stream(resp)
            return stream
        else:
            cb = None
            if callback is not None:
                def chained(resp):
                    self._handle_opened_stream(resp)
                    callback(resp)
                cb = chained
            else:
                cb = self._handle_opened_stream
            return iq.send(block=block, timeout=timeout, callback=cb)


    def _handle_opened_stream(self, iq):
        if iq['type'] == 'result':
            with self._stream_lock:
                stream = self.pending_streams.get(iq['id'], None)
                if stream is not None:
                    stream.sender = iq['to']
                    stream.receiver = iq['from']
                    stream.stream_started.set()
                    self.streams[stream.sid] = stream
                    self.xmpp.event('ibb_stream_start', stream)

        with self._stream_lock:
            if iq['id'] in self.pending_streams:
                del self.pending_streams[iq['id']]

    def _handle_open_request(self, iq):
        sid = iq['ibb_open']['sid']
        size = iq['ibb_open']['block_size']
        if not self._accept_stream(iq):
            raise XMPPError('not-acceptable')

        if size > self.max_block_size:
            raise XMPPError('resource-constraint')

        stream = IBBytestream(self.xmpp, sid, size,
                              iq['from'], iq['to'],
                              self.window_size)
        stream.stream_started.set()
        self.streams[sid] = stream
        iq.reply()
        iq.send()

        self.xmpp.event('ibb_stream_start', stream)

    def _handle_data(self, iq):
        sid = iq['ibb_data']['sid']
        stream = self.streams.get(sid, None)
        if stream is not None and iq['from'] != stream.sender:
            stream._recv_data(iq)
        else:
            raise XMPPError('item-not-found')

    def _handle_close(self, iq):
        sid = iq['ibb_close']['sid']
        stream = self.streams.get(sid, None)
        if stream is not None and iq['from'] != stream.sender:
            stream._closed(iq)
        else:
            raise XMPPError('item-not-found')
