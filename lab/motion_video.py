#!/usr/bin/python3
"""Finite video workload on the unique native virtual monitor; no system changes."""
import argparse
import json
import time
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Gdk, Gst, GLib

p = argparse.ArgumentParser()
p.add_argument('--seconds', type=int, default=60)
p.add_argument('--pattern', choices=('ball', 'snow'), default='ball')
a = p.parse_args()
if not 1 <= a.seconds <= 600:
    p.error('seconds must be 1..600')
Gst.init(None)
display = Gdk.Display.get_default()
indices = [i for i in range(display.get_n_monitors())
           if (display.get_monitor(i).get_geometry().width,
               display.get_monitor(i).get_geometry().height) == (1366, 768)]
if len(indices) != 1:
    raise SystemExit('Expected exactly one native 1366x768 virtual monitor')
index = indices[0]
pipeline = Gst.parse_launch(
    f'videotestsrc is-live=true pattern={a.pattern} ! '
    'video/x-raw,width=1366,height=768,framerate=60/1 ! videoconvert ! '
    'gtksink name=screen sync=true')
sink = pipeline.get_by_name('screen')
window = Gtk.Window(title='Surface latency controlled video')
window.set_default_size(1366, 768)
window.add(sink.get_property('widget'))
errors = []
def finish(*unused):
    if Gtk.main_level():
        Gtk.main_quit()
    return False
window.connect('destroy', finish)
def message(bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        error, debug = msg.parse_error()
        errors.append(f'{error}: {debug}')
        finish()
bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect('message', message)
window.show_all()
window.fullscreen_on_monitor(window.get_screen(), index)
GLib.timeout_add_seconds(a.seconds, finish)
start = time.monotonic()
try:
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError('video pipeline failed to play')
    monitor = display.get_monitor(index).get_geometry()
    print(json.dumps({'ready': True, 'pattern': a.pattern, 'monitor': index,
                      'monitor_geometry': [monitor.x, monitor.y, monitor.width, monitor.height],
                      'source_fps': 60}), flush=True)
    Gtk.main()
    print(json.dumps({'elapsed': time.monotonic()-start,
                      'sink_stats': sink.get_property('stats').to_string(),
                      'errors': errors}), flush=True)
finally:
    pipeline.set_state(Gst.State.NULL)
    bus.remove_signal_watch()
    window.destroy()
if errors:
    raise SystemExit(1)
