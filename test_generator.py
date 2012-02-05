#!/usr/bin/env python

import pbs

g = pbs.ifconfig(_generator=True)
for line in g.generate_stdout():
    print line.rstrip()
g.process.wait()

lines = "\n".join(["abcdefghijklmnopqrstuvwxyz0123456789"] * 100)
g = pbs.cat(_generator=True)
g.set_input(lines)
for line in g.generate_stdout():
    line = line.rstrip()
    print line[:10] + " PASSTHROUGH " + line[10:]
g.process.wait()

g = pbs.iostat("disk0", "1", _generator=True)
for line in g.generate_stdout():
    line = line.rstrip()
    print line[:10] + " Continuous " + line[10:]

g.process.wait()