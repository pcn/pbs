#!/usr/bin/env python

import pbs

g = pbs.ifconfig(_generate=True)
for line in g.generate_stdout():
    print line.rstrip()

g = pbs.iostat("disk0", "1", _generate=True)
for line in g.generate_stdout():
    line = line.rstrip()
    print line[:10] + " HAHA " + line[10:]

