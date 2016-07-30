# Copyright 2016 Fox Wilson
# Usage of the works is permitted provided that this instrument is retained
# with the works, so that any entity that uses the works is notified of this
# instrument. DISCLAIMER: THE WORKS ARE WITHOUT WARRANTY.

import collections
import contextlib
import copy
import functools
import mido
import os
import pickle
import stuf
import threading
import time
import uuid

controllers = collections.defaultdict(threading.Event)
inverse_controllers = collections.defaultdict(threading.Event)
notes = collections.defaultdict(threading.Event)

state = stuf.stuf({"inp": None, "out": None, "threads": [], "handlers": {}, "tempo": 120, "tsig": 4, "loops": [], "metronome": None})

# Utilities

def make_list(x):
    if isinstance(x, list):
        return x
    elif x is None:
        return []
    return [x]

def program(channel, program):
    state.out.send(mido.Message("program_change", channel=channel, program=program))

def wait_measures(n):
    time.sleep((60 / state.tempo) * state.tsig * n)

def send_factory(t, e):
    def send():
        time.sleep(t)
        try: state.out.send(e)
        except: pass
    return send

def out_event(e):
    if e.time > 0:
        threading.Thread(target=send_factory(e.time * (60 / state.tempo), e)).start()
    else:
        try: state.out.send(e)
        except: pass

# Proxies

class OpaqueProxy:
    def __init__(self, initial):
        self.value = initial

    def __call__(self, value):
        self.value = value

class RecordedMessage(mido.Message):
    def __init__(self, message):
        self.__dict__.update(message.__dict__)

    def copy(self, *args, **kwargs):
        return RecordedMessage(super(RecordedMessage, self).copy(*args, **kwargs))

def _(x):
    if isinstance(x, OpaqueProxy):
        return x.value

    return x

# Event bus

class EventBus:
    def __init__(self):
        self.handlers = {}
        self.pipelines = []

    def fire(self, x):
        if not isinstance(x, list):
            x = [x]

        for pipeline in self.pipelines:
            if not x:
                break
            result = []
            for i in x:
                events = pipeline.process(i)
                result.extend(make_list(events))
            x = result

        if x:
            for handler in list(self.handlers.values()):
                for i in x:
                    handler(i)

    def register(self, f):
        id = str(uuid.uuid4())
        self.handlers[id] = f
        return id

    def unregister(self, id):
        del self.handlers[id]

event = EventBus()
event.register(out_event)

@contextlib.contextmanager
def event_handler(f):
    id = event.register(f)
    yield
    event.unregister(id)

# Event pipelining

def valid_events_only(e):
    if e.type in ["note_on", "note_off"]:
        if e.note < 0 or e.note > 127:
            return None

    return e

class Pipeline:
    def __init__(self, funcs=[]):
        self.funcs = copy.copy(funcs)
        self.post_funcs = [valid_events_only]

    def add(self, f):
        self.funcs.append(f)

    def pop(self, *args):
        return self.funcs.pop(*args)

    def process(self, x):
        continue_processing = True

        for f in self.funcs:
            if not x:
                break
            result = []
            x = make_list(x)
            for i in x:
                r = f(i)

                if isinstance(r, tuple) and len(r) == 2:
                    processed, continue_processing = r
                else:
                    processed = r

                result.extend(make_list(processed))
            x = result

            if not continue_processing:
                break
        return make_list(x)

def notes_only(f):
    @functools.wraps(f)
    def wrapped(e):
        if e.type not in ["note_on", "note_off"]:
            return e
        return f(e)
    return wrapped

## Default pipeline
default_pipeline = Pipeline()
event.pipelines.append(default_pipeline)

## Useful filters
channel_filter = lambda channel=0: lambda e: e if e.channel == _(channel) else None
channel_setter = lambda channel=3: lambda e: e.copy(channel=channel)
transposer = lambda amount: notes_only(lambda e: e.copy(note=e.note + _(amount)))
velocity_multiplier = lambda factor: lambda e: e if e.type != "note_on" else e.copy(velocity=int(e.velocity * factor))

@notes_only
def octave(e):
    return [e, e.copy(note=e.note+12)]

transpose = OpaqueProxy(0)

default_pipeline.add(transposer(transpose))

### misbehaving keyboard support
default_pipeline.add(lambda e: None if e.channel != 0 and not isinstance(e, RecordedMessage) else e)

# MIDI pair context management

def open_pair(input, output):
    if not isinstance(input, mido.ports.BaseInput):
        state.inp = mido.open_input([i for i in mido.get_input_names() if i.lower().startswith(input.lower())][0])
    else:
        state.inp = input
    if not isinstance(output, mido.ports.BaseOutput):
        state.out = mido.open_output([i for i in mido.get_output_names() if i.lower().startswith(output.lower())][0])
    else: state.out = output
    setup_threads()
    state.metronome = Metronome()

def close_pair():
    for thread in state["threads"]:
        thread.stop_flag.set()
    state.threads.clear()

    state.inp.close()
    state.inp = None

    state.out.close()
    state.out = None

@contextlib.contextmanager
def midi_pair(input="", output=""):
    open_pair(input, output)
    yield
    close_pair()

# Tempo context management

@contextlib.contextmanager
def tempo(t):
    old_tempo = state.tempo
    state.tempo = t
    yield
    state.tempo = old_tempo

# State tracking

def state_track(e):
    if e.channel != 0:
        return

    if e.type == "control_change":
        if e.value:
            controllers[e.control].set()
            inverse_controllers[e.control].clear()
        else:
            controllers[e.control].clear()
            inverse_controllers[e.control].set()
    if e.type == "note_on":
        notes[e.note].set()
        notes[e.note].last_velocity = e.velocity
    if e.type == "note_off":
        notes[e.note].clear()

event.register(state_track)

# Thread management

def thread_with_stop(target, args=[]):
    stop = threading.Event()
    args = list(args) + [stop]
    t = threading.Thread(target=target, args=args)
    t.stop_flag = stop
    state.threads.append(t)
    return t

def setup_threads():
    def send_messages(port, fire, stop):
        while True:
            for msg in state.inp.iter_pending():
                fire(msg)
            if stop.is_set():
                return
            time.sleep(0.01)

    t = thread_with_stop(send_messages, args=(state.inp, event.fire))
    t.start()

# Loop management

class Metronome:
    def __init__(self):
        self.on_measure = []
        self.on_beat = []
        self.count = 0
        self.audible = False

        t = thread_with_stop(target=self.start)
        t.start()

    def beat(self):
        seconds_per_beat = 60 / state.tempo
        self.count += 1
        self.count %= state.tsig

        if self.count == 0:
            for i in self.on_measure:
                i()
            if self.audible:
                state.out.send(mido.Message('note_on', channel=9, note=34, velocity=127))
        else:
            for i in self.on_beat:
                i()
            if self.audible:
                state.out.send(mido.Message('note_on', channel=9, note=33, velocity=100))

        time.sleep(seconds_per_beat)

        if self.audible:
            state.out.send(mido.Message('note_off', channel=9, note=75))
            state.out.send(mido.Message('note_off', channel=9, note=76))

    def start(self, stop):
        while True:
            if stop.is_set():
                return
            self.beat()

class Loop:
    def __init__(self, length, immediate=True, istate=0):
        self.length = length
        self.immediate_play = immediate
        self.state = istate
        self.set_defaults()

    def set_defaults(self):
        self.measures = 0
        self.recording = False
        self.really_recording = False
        self.playing = False
        self.has_played = False
        self.last_ts = None
        self.notes = []
        self.done = threading.Event()
        self.thread = None
        self.pipeline = Pipeline()
        self.channel = 0
        state.metronome.on_measure.append(self.measure)

    def __getstate__(self):
        return dict(length=self.length, notes=self.notes, channel=self.channel)

    def __setstate__(self, data):
        self.set_defaults()
        self.length = data["length"]
        self.notes = data["notes"]
        self.immediate_play = False
        self.measures = -1
        self.set_channel(data["channel"])

    def apply(self, f):
        self.notes = [f(i) for i in self.notes]

    def save(self, name):
        path = os.path.expanduser("~/.mt2/{}.loop".format(name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(name):
        with open(os.path.expanduser("~/.mt2/{}.loop".format(name)), "rb") as f:
            return pickle.load(f)

    def set_offset(self, n):
        self.notes[0].time = n

    def measure(self):
        if self.recording:
            if self.really_recording:
                self.measures += 1
                if self.measures >= self.length:
                    self.done.set()

            else:
                self.state += 1
                if self.state == 2:
                    self.last_ts = time.time()
                    self.really_recording = True
                    threading.Thread(target=self.really_record).start()

        elif self.playing:
            self.measures += 1
            if self.measures % self.length == 0 and (self.measures >= self.length or not self.immediate_play):
                if self.thread is not None:
                    state.threads.remove(self.thread)
                self.thread = thread_with_stop(target=self.play_once)
                self.thread.start()

    def note(self, e):
        if isinstance(e, RecordedMessage):
            return

        seconds = time.time() - self.last_ts
        beats_per_second = state.tempo / 60
        beats = seconds * beats_per_second

        self.notes.append(e.copy(time=beats))
        self.last_ts += seconds

    def record(self):
        state.metronome.audible = True
        self.recording = True
        return self

    def really_record(self):
        print("Recording started (will record for next {} measures).".format(self.length))

        for control in controllers:
            if controllers[control].is_set():
                self.notes.append(mido.Message("control_change", channel=0, control=control, value=127))

        for note in notes:
            if notes[note].is_set():
                self.notes.append(mido.Message("note_on", channel=0, note=note, velocity=notes[note].last_velocity))

        with event_handler(self.note):
            self.done.wait()

        t = (time.time() - self.last_ts) * (state.tempo / 60)

        for note in notes:
            if notes[note].is_set():
                self.notes.append(mido.Message("note_off", channel=0, note=note, time=t))

        # for control in controllers:
        #     if controllers[control].is_set():
        #         self.notes.append(mido.Message("control_change", channel=0, control=control, value=0))

        self.recording = self.really_recording = False
        self.state = 0
        print("Done recording.")
        state.metronome.audible = False

        if self.immediate_play:
            self.play()
            self.measures = 0
            self.thread = thread_with_stop(target=self.play_once)
            self.thread.start()

    def sync(self, other):
        self.measures = other.measures
        self.playing = True
        return self

    def play(self):
        self.measures = -1
        self.playing = True
        return self

    def stop(self):
        self.playing = False
        self.immediate_play = False

    def stop_now(self):
        self.playing = False
        self.thread.stop_flag.set()
        self.immediate_play = False

    def play_once(self, stop):
        for idx, i in enumerate(self.notes):
            if stop.is_set():
                return
            time.sleep((60 / state.tempo) * i.time)
            for e in self.pipeline.process(i):
                event.fire(RecordedMessage(e.copy(time=0)))

    def set_channel(self, channel):
        self.channel = channel
        self.pipeline.add(channel_setter(channel))
        return self

# Keep track of programs

class ProgramSet:
    def __init__(self, *programs):
        self.programs = programs

    def apply(self):
        for channel, prog in self.programs:
            program(channel, prog)
        return self

    def __setstate__(self):
        self.apply()

    def save(self, name):
        path = os.path.expanduser("~/.mt2/{}.programset".format(name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(name):
        with open(os.path.expanduser("~/.mt2/{}.programset".format(name)), "rb") as f:
            return pickle.load(f)
