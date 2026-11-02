"""Reconnectable Linux keyboards: legacy mini A/B, any keyboard Ctrl+Alt+A/B."""
import fcntl
import os
from pathlib import Path
import re
import select
import struct
import time

KEYS = {30:'a',48:'b',46:'c',32:'d',28:'enter',96:'enter'}


def keyboards(text):
    found = []
    for block in text.split('\n\n'):
        bits = re.search(r'^B: KEY=(.+)$',block,re.M)
        name = re.search(r'^N: Name="(.*)"$',block,re.M)
        handlers = re.search(r'^H: Handlers=(.+)$',block,re.M)
        if not all((bits,name,handlers)): continue
        word_bits=struct.calcsize('L')*8
        mask=sum(int(word,16) << (word_bits*i) for i,word in enumerate(reversed(bits.group(1).split())))
        if not all(mask & (1 << k) for k in (28,30,48)): continue
        for event in handlers.group(1).split():
            if re.fullmatch(r'event\d+',event): found.append(('/dev/input/'+event,name.group(1)))
    return found


class Keyboard:
    EVENT = struct.Struct('llHHi')
    def __init__(self):
        self.handles = {}; self.pressed = {}; self.last_scan = 0; self.last_key = 0
        self.minimum = time.time(); self.refresh()

    def refresh(self):
        self.last_scan = time.monotonic()
        try: current = dict(keyboards(Path('/proc/bus/input/devices').read_text()))
        except OSError: current = {}
        for fd,(path,name,grab) in list(self.handles.items()):
            if path not in current: self._drop(fd)
        for path,name in current.items():
            if path in [row[0] for row in self.handles.values()]: continue
            fd=None
            try:
                fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
                # Keep the original dedicated mini keypad from also typing into X11.
                grab='MINI_KEYBOARD Keyboard' in name
                if grab: fcntl.ioctl(fd,0x40044590,1)
                self.handles[fd]=(path,name,grab);self.pressed[fd]=set()
            except OSError:
                if fd is not None: os.close(fd)

    def _drop(self,fd):
        try:
            if self.handles[fd][2]: fcntl.ioctl(fd,0x40044590,0)
        except OSError: pass
        try: os.close(fd)
        except OSError: pass
        self.handles.pop(fd,None);self.pressed.pop(fd,None)

    @property
    def names(self): return [row[1] for row in self.handles.values()]

    def discard(self):
        self.minimum=time.time()
        for fd in list(self.handles):
            try:
                while select.select([fd],[],[],0)[0]:
                    if not os.read(fd,self.EVENT.size*128): self._drop(fd);break
                self.pressed.get(fd,set()).clear()
            except OSError: self._drop(fd)

    def read(self,timeout=.1):
        if time.monotonic()-self.last_scan>2:self.refresh()
        if not self.handles:time.sleep(timeout);return None
        try: ready=select.select(list(self.handles),[],[],timeout)[0]
        except OSError:self.refresh();return None
        for fd in ready:
            try:data=os.read(fd,self.EVENT.size*128)
            except OSError:self._drop(fd);continue
            if not data:self._drop(fd);continue
            for offset in range(0,len(data)-self.EVENT.size+1,self.EVENT.size):
                sec,usec,kind,code,value=self.EVENT.unpack_from(data,offset)
                if kind!=1:continue
                held=self.pressed[fd]
                if value==1:held.add(code)
                elif value==0:held.discard(code)
                allowed=self.handles[fd][2] or (bool(held & {29,97}) and bool(held & {56,100}))
                when=sec+usec/1e6
                if allowed and value==1 and code in KEYS and when>=self.minimum and when-self.last_key>.25:
                    self.last_key=when;return KEYS[code]
        return None

    def close(self):
        for fd in list(self.handles):self._drop(fd)
