#===============================================================================
# Copyright (C) 2011-2012 by Andrew Moffat
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#===============================================================================



import subprocess as subp
import inspect
import sys
import traceback
import os
import re
from glob import glob
import shlex
import warnings
import select
import traceback



__version__ = "0.75"
__project_url__ = "https://github.com/amoffat/pbs"

IS_PY3 = sys.version_info[0] == 3
if IS_PY3: raw_input = input




class ErrorReturnCode(Exception):
    truncate_cap = 200

    def __init__(self, full_cmd, stdout, stderr):
        self.full_cmd = full_cmd
        self.stdout = stdout
        self.stderr = stderr

        if self.stdout is None: tstdout = "<redirected>"
        else:
            tstdout = self.stdout[:self.truncate_cap] 
            out_delta = len(self.stdout) - len(tstdout)
            if out_delta: 
                tstdout += "... (%d more, please see e.stdout)" % out_delta
            
        if self.stderr is None: tstderr = "<redirected>"
        else:
            tstderr = self.stderr[:self.truncate_cap]
            err_delta = len(self.stderr) - len(tstderr)
            if err_delta: 
                tstderr += "... (%d more, please see e.stderr)" % err_delta

        msg = "\n\nRan: %r\n\nSTDOUT:\n\n  %s\n\nSTDERR:\n\n  %s" %\
            (full_cmd, tstdout, tstderr)
        super(ErrorReturnCode, self).__init__(msg)

class CommandNotFound(Exception): pass

rc_exc_regex = re.compile("ErrorReturnCode_(\d+)")
rc_exc_cache = {}

def get_rc_exc(rc):
    try: return rc_exc_cache[rc]
    except KeyError: pass
    
    name = "ErrorReturnCode_%d" % rc
    exc = type(name, (ErrorReturnCode,), {})
    rc_exc_cache[name] = exc
    return exc




def which(program):
    def is_exe(fpath):
        return os.path.exists(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program): return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None

def resolve_program(program):
    path = which(program)
    if not path:
        # our actual command might have a dash in it, but we can't call
        # that from python (we have to use underscores), so we'll check
        # if a dash version of our underscore command exists and use that
        # if it does
        if "_" in program: path = which(program.replace("_", "-"))        
        if not path: return None
    return path


class Command(object):
    prepend_stack = []

    @classmethod
    def create(cls, program, raise_exc=True):
        path = resolve_program(program)
        if not path:
            if raise_exc: raise CommandNotFound(program)
            else: return None
        return cls(path)
    
    def __init__(self, path):            
        self.path = path
        
        self.process = None
        self._input = None
        self._stdout = None
        self._stderr = None
        self._stdout_buffer = list()
        self._stderr_buffer = list()
        
        self.call_args = {
            "bg": False, # run command in background
            "with": False, # prepend the command to every command after it
            "out": None, # redirect STDOUT
            "err": None, # redirect STDERR
            "err_to_out": None, # redirect STDERR to STDOUT
            "generator": None, # use stdout and stderr as generators
        }
        
    def __getattr__(self, p):
        return getattr(str(self), p)

    def set_input(self, var):
        self._input = var
        
    @property
    def stdout(self):
        if self.call_args["bg"]: self.wait()
        if self.call_args["generator"]:
            raise TypeError("generator requested, can't use stdout'")
        return self._stdout

    @property
    def stderr(self):
        if self.call_args["bg"]: self.wait()
        if self.call_args["generator"]:
            raise TypeError("generator requested, can't use stderr'")
        return self._stderr


    def generate_stdout(self, bufsize=1024, sep='\n'):
        if not self.call_args["generator"]:
            raise TypeError("generator not requested, can't call generate_stdout()")
        for line in self._generate_stdfds_line(bufsize, sep, STDOUT=True):
            yield line

    def generate_stderr(self, bufsize=1024, sep='\n'):
        for line in self._generate_stdfds_line(bufsize, sep, STDERR=True):
            yield line
        if not self.call_args["generator"]:
            raise TypeError("generator not requested, can't call generate_stderr()")
            
    def _generate_stdfds_line(self, bufsize, sep, STDOUT=False, STDERR=False):
        """There are whole families of commands that generate data,
        e.g. vmstat, tcpdump etc. It's silly how difficult it is to
        use these with python's subprocess module, so deal with them
        by selecting on file descriptors

        split_char is the character that the stream will be split on.

        """
        gen         = self._generate_fds()
        source_done = False
        sout        = None
        serr        = None
        while True:
            # Check to see if the generator is done, and stop using it
            # if it is.  Continuing to blindly try it can lead to
            # StopIteration maybe getting propogated?  Anyway, this
            # works.
            if source_done is False:
                try:
                    (sout,serr) = gen.next()
                    if sout:
                        self._stdout_buffer.append(sout)
                    if serr:
                        self._stderr_buffer.append(serr)
                except StopIteration:
                    # only raised when both fds are closed
                    source_done = True

            if STDOUT:
                val = self._extract_line(self._stdout_buffer, bufsize, sep)
                if len(val) > 0:
                    yield val
                if not self._stdout_buffer and not sout:
                    return
            elif STDERR:
                val = self._extract_line(self._stderr_buffer, bufsize, sep)
                if len(val) > 0:
                    yield val
                if not self._stderr_buffer and not serr:
                    return

    def _extract_line(self, fd_buffer, bufsize, sep):
        """Manages the fd_buffer, returning either:
        1) a line of data (if sep is encountered), including
           sep at the end.
        2) a buffer of bufsize or less (less if the fd was closed, and
           less data was read but no sep was encountered)
        """
        b = fd_buffer
        sl = len(sep)
        line = None

        def trim_car(offset):
            retstr = b[0][0:offset+sl]
            b[0]   = b[0][offset+sl:] # shrink the string in the car
            if len(b[0]) == 0:
                # If the car is an empty string, get rid of it
                del(b[0])
            return retstr

        # null case
        if len(b) == 0: return ""
        if b[0] is None: return ""
            
        # first case, separator string is in the first string in the buffer
        next_sep = b[0].find(sep)
        bullpen = list()
        for hold in b:
            if hold.find(sep) > -1: 
                break
            else:
                bullpen.append(hold)
        else:
            # document what we want to do here better.  I'm worried
            # about inducing deadlock with >1 fd being juggled
            line = "".join(b)
            del(b[:])
            # need to clarify whether we're talking bytes or characters for python3
            if len(line) > bufsize:
                b.append(line[bufsize:])
                line = line[0:bufsize]
            return line
        # Now bullpen has strings leading up to the string with the separator.
        if len(bullpen) > 0: 
            del(fd_buffer[0:len(bullpen)])
        tail = trim_car(hold.find(sep))
        bullpen.append(tail)
        line = "".join(bullpen)
        return line

    def _generate_fds(self):
        """Can't have reading on e.g. stdout stuck because it's
        waiting for stderr to be read from.  So read from stdout and
        stderr at the same time, and buffer to their respective buffers
        in the caller.

        Maybe change this to allow an input generator, but here there
        be dragons.  The current use case is to have a process that
        creates output, but doesn't require input from us.  Having
        input into the same process creates a big opportunity for
        deadlock, I think.

        Yields (stdout, stderr) output.

        Note the input can't be a generator itself at this point.
        """
        read_set     = []
        write_set    = []
        stdin        = None
        stdout       = None # Return
        stderr       = None # Return
        proc         = self.process
        input_offset = 0

        if proc.stdin and (not proc.stdin.closed):
            # Flush stdio buffer.  This might block, if the user has
            # been writing to .stdin in an uncontrolled fashion.
            proc.stdin.flush()
            if self._input:
                write_set.append(proc.stdin)
            else:
                proc.stdin.close()
        if proc.stdout and (not proc.stdout.closed):
            read_set.append(proc.stdout)
            stdout = []
        else: print "no stdout? {0}".format(proc.stdout)
        if proc.stderr and (not proc.stderr.closed):
            read_set.append(proc.stderr)
            stderr = []

        # if not read_set:
        #     print "stopping iteration from not read_set"
        #     raise StopIteration

        try: # python 2.7/3.2
            PIPE_BUF = select.PIPE_BUF
        except AttributeError: # older versions
            PIPE_BUF=512

        count = 0
        while True:
            if not read_set and not write_set:
                print read_set, write_set
                print "stopping iteration from not read_set and not write_set"
                raise StopIteration
            try:
                # .1 second timeout - looping shoudln't be costly, right?
                rlist, wlist, xlist = select.select(read_set, write_set, [])
            except select.error, e:
                if e.args[0] == errno.EINTR:
                    continue
                raise

            if proc.stdin in wlist:
                # When select has indicated that the file is writable,
                # we can write up to PIPE_BUF bytes without risk of
                # blocking.  
                chunk = self._input[input_offset : input_offset + PIPE_BUF]
                # TODO: handle input pipes and generators?
                bytes_written = os.write(proc.stdin.fileno(), chunk)
                input_offset += bytes_written
                if input_offset >= len(self._input):
                    proc.stdin.close()
                    write_set.remove(proc.stdin)
                
            if proc.stdout in rlist:
                data = os.read(proc.stdout.fileno(), 1024)
                if data == "": # empty read, fd is empty, close
                    proc.stdout.close()
                    read_set.remove(proc.stdout)
                stdout.append(data)
            if proc.stderr in rlist:
                data = os.read(proc.stderr.fileno(), 1024)
                if data == "":
                    proc.stderr.close()
                    read_set.remove(proc.stderr)
                stderr.append(data)
            if rlist:
                yield "".join(stdout), "".join(stderr)
                del(stdout[:])
                del(stderr[:])
        
        
    def wait(self):
        if self.process.returncode is not None: return
        self._stdout, self._stderr = self.process.communicate()
        rc = self.process.wait()

        if rc != 0: raise get_rc_exc(rc)(self.stdout, self.stderr)
        return self
    
    def __repr__(self):
        return str(self)

    def __long__(self):
        return long(str(self).strip())

    def __float__(self):
        return float(str(self).strip())

    def __int__(self):
        return int(str(self).strip())
        
    def __str__(self):
        if IS_PY3: return self.__unicode__()
        else: return unicode(self).encode("utf-8")
        
    def __unicode__(self):
        if self.process: 
            if not self.call_args["generator"] and self.stdout:
                return self.stdout.decode("utf-8") # byte string
            else: return ""
        else: return self.path

    def __enter__(self):
        if not self.call_args["with"]: Command.prepend_stack.append([self.path])

    def __exit__(self, typ, value, traceback):
        if Command.prepend_stack: Command.prepend_stack.pop()

    def __call__(self, *args, **kwargs):
        kwargs = kwargs.copy()
        args = list(args)
        stdin = None
        processed_args = []
        cmd = []

        # aggregate any with contexts
        for prepend in self.prepend_stack: cmd.extend(prepend)
        
        cmd.append(self.path)
        
        # pull out the pbs-specific arguments (arguments that are not to be
        # passed to the commands
        for parg, default in self.call_args.items():
            key = "_" + parg
            self.call_args[parg] = default
            if key in kwargs:
                self.call_args[parg] = kwargs[key] 
                del kwargs[key]
                
        # check if we're piping via composition
        stdin = subp.PIPE
        actual_stdin = None
        if args:
            first_arg = args.pop(0)
            if isinstance(first_arg, Command):
                # it makes sense that if the input pipe of a command is running
                # in the background, then this command should run in the
                # background as well
                if first_arg.call_args["bg"]:
                    self.call_args["bg"] = True
                    stdin = first_arg.process.stdout
                else:
                    actual_stdin = first_arg.stdout
            else: args.insert(0, first_arg)
        
        # aggregate the position arguments
        for arg in args:
            # i should've commented why i did this, because now i don't
            # remember.  for some reason, some command was more natural
            # taking a list?
            if isinstance(arg, (list, tuple)):
                for sub_arg in arg: processed_args.append(str(sub_arg))
            else: processed_args.append(str(arg))


        # aggregate the keyword arguments
        for k,v in kwargs.items():
            # we're passing a short arg as a kwarg, example:
            # cut(d="\t")
            if len(k) == 1:
                if v is True: arg = "-"+k
                else: arg = "-%s %r" % (k, v)

            # we're doing a long arg
            else:
                k = k.replace("_", "-")

                if v is True: arg = "--"+k
                else: arg = "--%s=%s" % (k, v)
            processed_args.append(arg)

        # makes sure our arguments are broken up correctly
        split_args = shlex.split(" ".join(processed_args))

        # now glob-expand each arg and compose the final list
        final_args = []
        for arg in split_args:
            expanded = glob(arg)
            if expanded: final_args.extend(expanded)
            else: final_args.append(arg)

        cmd.extend(final_args)
        # for debugging
        self._command_ran = " ".join(cmd)
        

        # with contexts shouldn't run at all yet, they prepend
        # to every command in the context
        if self.call_args["with"]:
            Command.prepend_stack.append(cmd)
            return self
        
        
        # stdout redirection
        stdout = subp.PIPE
        out = self.call_args["out"]
        if out:
            if isinstance(out, file): stdout = out
            else: stdout = file(str(out), "w")
        
        # stderr redirection
        stderr = subp.PIPE
        err = self.call_args["err"]
        if err:
            if isinstance(err, file): stderr = err
            else: stderr = file(str(err), "w")
            
        if self.call_args["err_to_out"]: stderr = stdout
            

        # leave shell=False
        self.process = subp.Popen(cmd, shell=False, env=os.environ,
            stdin=stdin, stdout=stdout, stderr=stderr)
        # we're running in the background, return self and let us lazily
        # evaluate
        if self.call_args["bg"]: return self

        if self.call_args["generator"]:
            # The caller wants a stream of input, so must call self.wait() explicitly
            # after StopIteration is raised on the fd's that want it.
            return self
        else:
            # run and block
            self._stdout, self._stderr = self.process.communicate(actual_stdin)
            rc = self.process.wait()

            if rc != 0: raise get_rc_exc(rc)(self._command_ran, self.stdout, self.stderr)
            return self



# this class is used directly when we do a "from pbs import *".  it allows
# lookups to names that aren't found in the global scope to be searched
# for as a program.  for example, if "ls" isn't found in the program's
# scope, we consider it a system program and try to find it.
class Environment(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        
        self["Command"] = Command
        self["CommandNotFound"] = CommandNotFound
        self["ErrorReturnCode"] = ErrorReturnCode
        self["ARGV"] = sys.argv[1:]
        for i, arg in enumerate(sys.argv):
            self["ARG%d" % i] = arg
        
        # this needs to be last
        self["env"] = os.environ
        
    def __setitem__(self, k, v):
        # are we altering an environment variable?
        if "env" in self and k in self["env"]: self["env"][k] = v
        # no?  just setting a regular name
        else: dict.__setitem__(self, k, v)
        
    def __missing__(self, k):
        # the only way we'd get to here is if we've tried to
        # import * from a repl.  so, raise an exception, since
        # that's really the only sensible thing to do
        if k == "__all__":
            raise RuntimeError("Cannot import * from the commandline, please \
see \"Limitations\" here: %s" % __project_url__)

        # if we end with "_" just go ahead and skip searching
        # our namespace for python stuff.  this was mainly for the
        # command "id", which is a popular program for finding
        # if a user exists, but also a python function for getting
        # the address of an object.  so can call the python
        # version by "id" and the program version with "id_"
        if not k.endswith("_"):
            # check if we're naming a dynamically generated ReturnCode exception
            try: return rc_exc_cache[k]
            except KeyError:
                m = rc_exc_regex.match(k)
                if m: return get_rc_exc(int(m.group(1)))
                
            # are we naming a commandline argument?
            if k.startswith("ARG"):
                return None
                
            # is it a builtin?
            try: return getattr(self["__builtins__"], k)
            except AttributeError: pass
        elif not k.startswith("_"): k = k.rstrip("_")
        
        # how about an environment variable?
        try: return os.environ[k]
        except KeyError: pass
        
        # is it a custom builtin?
        builtin = getattr(self, "b_"+k, None)
        if builtin: return builtin
        
        # it must be a command then
        return Command.create(k)
    
    
    def b_echo(self, *args, **kwargs):
        out = Command("echo")(*args, **kwargs)
        # no point printing if we're redirecting...
        if out.stdout is not None: print(out)
        return out
    
    def b_cd(self, path):
        os.chdir(path)
        
    def b_which(self, program):
        return which(program)





def run_repl(env):
    banner = "\n>> PBS v{version}\n>> https://github.com/amoffat/pbs\n"
    
    print(banner.format(version=__version__))
    while True:
        try: line = raw_input("pbs> ")
        except (ValueError, EOFError): break
            
        try: exec(compile(line, "<dummy>", "single"), env, env)
        except SystemExit: break
        except: print(traceback.format_exc())

    # cleans up our last line
    print('')




# this is a thin wrapper around THIS module (we patch sys.modules[__name__]).
# this is in the case that the user does a "from pbs import whatever"
# in other words, they only want to import certain programs, not the whole
# system PATH worth of commands.  in this case, we just proxy the
# import lookup to our Environment class
class SelfWrapper(object):
    def __init__(self, self_module):
        self.self_module = self_module
        self.env = Environment(globals())
    
    def __getattr__(self, name):
        return self.env[name]



# we're being run as a stand-alone script, fire up a REPL
if __name__ == "__main__":
    globs = globals()
    f_globals = {}
    for k in ["__builtins__", "__doc__", "__name__", "__package__"]:
        f_globals[k] = globs[k]
    env = Environment(f_globals)
    run_repl(env)
    
# we're being imported from somewhere
else:
    frame, script, line, module, code, index = inspect.stack()[1]
    env = Environment(frame.f_globals)


    # are we being imported from a REPL?
    if script.startswith("<") and script.endswith(">") :
        self = sys.modules[__name__]
        sys.modules[__name__] = SelfWrapper(self)
        
    # we're being imported from a script
    else:

        # we need to analyze how we were imported
        with open(script, "r") as h: source = h.readlines()
        import_line = source[line-1]

        # this it the most magical choice.  basically we're trying to import
        # all of the system programs into our script.  the only way to do
        # this is going to be to exec the source in modified global scope.
        # there might be a less magical way to do this...
        if "*" in import_line:
            # do not let us import * from anywhere but a stand-alone script
            if frame.f_globals["__name__"] != "__main__":
                raise RuntimeError("Cannot import * from anywhere other than \
a stand-alone script.  Do a 'from pbs import program' instead. Please see \
\"Limitations\" here: %s" % __project_url__)

            warnings.warn("Importing * from pbs is magical and therefore has \
some limitations.  Please become familiar with them under \"Limitations\" \
here: %s  To avoid this warning, use a warning filter or import your \
programs directly with \"from pbs import <program>\"" % __project_url__,
RuntimeWarning, stacklevel=2)

            # we avoid recursion by removing the line that imports us :)
            source = "".join(source[line:])
        
            exit_code = 0
            try: exec(source, env, env)
            except SystemExit as e: exit_code = e.code
            except: print(traceback.format_exc())

            # we exit so we don't actually run the script that we were imported
            # from (which would be running it "again", since we just executed
            # the script with exec
            exit(exit_code)

        # this is the least magical choice.  we're importing either a
        # selection of programs or we're just importing the pbs module.
        # in this case, let's just wrap ourselves with a module that has
        # __getattr__ so our program lookups can be done there
        else:
            self = sys.modules[__name__]
            sys.modules[__name__] = SelfWrapper(self)
