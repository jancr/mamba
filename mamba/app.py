##############################################################################
# Copyright (c) 2010, 2011, Sune Frankild and Lars Juhl Jensen
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#  - Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#  - Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#  - Neither the name of the Novo Nordisk Foundation Center for Protein
#    Research, University of Copenhagen nor the names of its contributors may
#    be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
# THE POSSIBILITY OF SUCH DAMAGE.
##############################################################################

import os
import sys
import glob
import time
import datetime
import types
import Queue
import posix
import signal
import threading
import traceback

import mamba.util
import mamba.http
import mamba.task
import mamba.setup


class HTTPServer(mamba.util.Logger):
	
	def __init__(self):
		mamba.util.Logger.__init__(self, "server")
		self._network = None
		self._mainq   = None
		self._queues  = {}
		self._pools   = []
		self._wacher  = None
		self._deadman = None
		self._plugins = {}
		self._www_dir = None
		
	def _load_plugins(self):
		self._plugins = {}
		self._plugins["getstatus"] = getattr(mamba.task, "GetStatus")
		self._plugins["download"]  = getattr(mamba.task, "Download")
		if mamba.setup.config().server.plugins == None:
			self.war("No plugin directory specified in .ini file.")
			return
		dirname = os.path.abspath(mamba.setup.config().server.plugins)
		if dirname == os.path.abspath("./"):
			return
		if os.path.exists(dirname) and os.path.isdir(dirname):
			sys.path.append(os.path.abspath(dirname))
			for name in glob.glob(os.path.join(dirname, "*.py")):
				name = os.path.basename(name)
				if not name.startswith('__'):
					name = name.replace(".py", "")
					if name not in sys.modules and name != "mambasrv":
						self.info("Importing plugin '%s':" % name)
						__import__(name)
					else:
						self.info("Plugin '%s' was already imported - fine." % name)
					if name in sys.modules:
						module = sys.modules[name]
						for symbol in dir(module):
							if symbol.startswith('_') or symbol.endswith("Request"):
								continue
							type = getattr(module, symbol)
							try:
								if type in self._plugins:
									self.err("Plugin '%s' redefines class '%s'." % (str(module), str(type)))
								else:
									if isinstance(type, types.ClassType) and issubclass(type, mamba.task.Request):
										self._plugins[symbol.lower()] = type
										self.info(" - Loaded request handler: %s" % symbol)
							except TypeError, e:
								self.warn("Error analyzing type '%s'. Message: '%s'" % (str(type), str(e)))
		
	def _create_queue(self, queue_name, queue_type):
		if queue_name not in self._queues:
			if queue_type.lower() == "priority":
				self._queues[queue_name] = Queue.PriorityQueue()
			elif queue_type.lower() == "fifo":
				self._queues[queue_name] = Queue.Queue()
			else:
				raise Exception, "Cannot create a queue of type '%s'. Valid types are 'priority' and 'fifo'." % queue_type
			self.debug("Created queue: '%s'." % queue_name)
		else:
			raise Exception, "Cannot create gueue '%s' because it already exists." % queue_name
			
	def get_queue(self, queue_name):
		if queue_name in self._queues:
			return self._queues[queue_name]
		else:
			raise Exception, "The server does not have a queue named '%s'." % queue_name
		
	def get_thread_pools(self):
		return self._pools
		
	def get_queue_names(self):
		return self._queues.keys()
		
	def add_response(self, reply):
		self._network.add_response(reply)
	
	def initialize(self):
		self._www_dir = os.path.abspath(mamba.setup.config().server.www_dir)
		if mamba.setup.config().server.auto_restart:
			self._deadman = str("/tmp/deadman.%i" % os.getpid())
			try:
				f = open(self._deadman, "w")
				f.write("cd %s\n" % os.path.abspath("./"))
				f.write("%s\n" % (" ".join(sys.argv)))
				f.flush()
				f.close()
				self.info("Created dead-man file: '%s'" % self._deadman)
			except IOError, e:
				self.err("Could not create file: '%s'. Server cannot start." % self._deadman)
				raise e
		else:
			self.info("Auto-restart is disabled.")
		for queue_name in mamba.setup.config().queues:
			queue_type = mamba.setup.config().queues[queue_name]
			self._create_queue(queue_name, queue_type)
		if not "main" in mamba.setup.config().queues:
			self._create_queue("main", "priority")
		self._mainq = self.get_queue("main")
		for queue_name in mamba.setup.config().thread_pools:
			for pool in mamba.setup.config().thread_pools[queue_name]:
				thread_pool = ThreadPool(pool.name, self, queue_name, self.get_queue(queue_name), pool.threads)
				self._pools.append(thread_pool)
				thread_pool.bootup()
		self._load_plugins()
		self._wacher = mamba.util.ProcessWatcher(self)
		self._wacher.start()
		self._network = mamba.http.HTTPOperator(self._deadman)
		if mamba.setup.config().server.wait_on_port:
			while not self._network.initialize():
				time.sleep(10)
			return True
		else:
			return self._network.initialize()
				
	def tear_down_server(self, restart = False):
		self._network.tear_down()
		for thread_pool in self._pools:
			thread_pool.shutdown()
		for thread_pool in self._pools:
			thread_pool.terminate()
		if self._wacher:
			self._wacher.stop()
			self._wacher.join()
			self._wacher = None
		for queue_name in self._queues.keys():
			del self._queues[queue_name]
		self._queues = {}
		self._pools  = []
		
	def on_stop(self, signum=None, frame=None):
		if signum != None:
			self.warn("Server received SIGNAL", signum)
		self.info("Server is terminating ...")
		if self._network:
			self._network.shutdown()
			
	def _create_rewrite_task(self, http):
		if "REWRITE" in mamba.setup.config().sections:
			url = http.url.lower()
			for source in mamba.setup.config().sections["REWRITE"]:
				if url == source:
					destination = mamba.setup.config().sections["REWRITE"][source].strip()
					rewrite_url = url.replace(source, destination, 1)
					self.info("Client %s requested URL: '%s' rewritten to: '%s'" % (http.remote_ip, http.url, rewrite_url))
					return mamba.http.HTTPRedirect(mamba.task.Request(http), rewrite_url)
		return None
	
	def _create_proxy_task(self, http):
		if "PROXY" in mamba.setup.config().sections:
			url = http.url.lower()
			for source in mamba.setup.config().sections["PROXY"]:
				if url.startswith(source):
					destination = mamba.setup.config().sections["PROXY"][source].strip()
					proxy_url = url.replace(source, destination, 1)
					self.info("Client %s requested URL: '%s' proxied to: '%s'" % (http.remote_ip, http.url, proxy_url))
					return mamba.task.Proxy(http, proxy_url)
		return None
	
	def _create_getfile_task(self, http):
		www_file = os.path.normpath(os.path.abspath(self._www_dir + "/" + http.url))
		if www_file.startswith(self._www_dir):
			if os.path.isdir(www_file):
				index_file = os.path.normpath(www_file + "/index.html")
				if os.path.isfile(index_file):
					www_file = index_file
				else:
					index_file = os.path.normpath(www_file + "/index.html")
					if os.path.isfile(www_file):
						www_file = index_file
					else:
						www_file = None
			elif not os.path.isfile(www_file):
				www_file = None
		else:
			www_file = None
		if www_file:
			#self.info("Client %s requested local file: '%s'" % (http.remote_ip, www_file))
			return mamba.task.GetFile(http, www_file)
		return None
	
	def _create_request_task(self, http):
		actions = http.path.lower().split("/")
		actions.reverse()
		for action in actions:
			if action in self._plugins:
				return self._plugins[action](http)
		return None

	def create_task(self, http):
		task = self._create_rewrite_task(http)
		if task:
			return task
		
		task = self._create_proxy_task(http)
		if task:
			return task
		
		task = self._create_getfile_task(http)
		if task:
			return task
			
		task = self._create_request_task(http)
		if task:
			return task
		
		return mamba.http.HTTPErrorResponse(mamba.task.ErrorRequest(http), 400, "No plugins assigned to action: '%s'" % http.get_action())

	def run(self):
		if not self.initialize():
			self.tear_down_server()
			return
		continue_loop = True
		restart = False
		while continue_loop:
			try:
				http_requests = self._network.get_http_requests()
				if len(http_requests) == 0:
					break
				for http in http_requests:
					if self.show_debug:
						ip, ts, hs, bs, t = http.remote_ip, http.bytes_received, http.header_size, http.content_length, str(http.duration)
						self.debug("Got %i bytes from %s in %s seconds. Header/body was %i and %i bytes." % (ts, ip, t, hs, bs))
					try:
						task = self.create_task(http)
						if isinstance(task, mamba.task.Request):
							try:
								task.queue("main")
								self._mainq.put(task)
							except mamba.task.NextMethodNotDefined, e:
								reply = mamba.http.HTTPErrorResponse(task, 500, "Class '%s' has no method called 'main()'.")
								self._network.add_response(reply)
								
						elif isinstance(task, mamba.http.HTTPResponse):
							self._network.add_response(task)
							
					except Exception, e:
						self.err(traceback.format_exc())
						reply = mamba.http.HTTPErrorResponse(mamba.task.ErrorRequest(http), 500, str(e))
						self._network.add_response(reply)
			#
			# Break loop on ctrl+C.
			#
			except KeyboardInterrupt:
				sys.stdout.write("\r")
				self.info("Stopped by keyboard interrupt")
				if self._network._shutdown:
					posix._exit(1)
				else:
					self.on_stop()
				
			except Exception, e:
				self.err("Server loop caught unhandled error.")
				self.err(e)
				self.err(traceback.format_exc())
		if restart:
			self.on_stop()
		self.tear_down_server()
		if restart:
			self.run() # Calls initialize() before running server loop.
		else:
			self.info("Bye bye")
		sys.stderr.close()
		sys.stdout.close()
		posix._exit(0)


class Application:
	
	def __init__(self, server=HTTPServer()):
		self.inifile = None
		self.server = server
		if len(sys.argv) > 1:
			self.inifile = sys.argv[1]
			if not os.path.exists(self.inifile):
				sys.stderr.write("[Error]  Cannot locate Reflect ini-file at: ")
				sys.stderr.write(self.inifile)
				sys.stderr.write("\nPlease check the path to the ini file and retry.")
				sys.stderr.write("\nServer could not be started - over and out.")
				sys.exit(-1)
	
	def run(self):
		conf = mamba.setup.Configuration(self.inifile)
		if conf.server.plugins != None:
			print "[INIT]  Searching %s for a Setup-class." % conf.server.plugins
			dirname = os.path.abspath(conf.server.plugins)
			sys.path.append(os.path.abspath(dirname))
			for filename in glob.glob(os.path.join(dirname, "*.py")):
				name = os.path.basename(filename)
				if not name.startswith('__'):
					name = name.replace(".py", "")
					if name not in sys.modules and name != "mambasrv":
						print "[INIT]  Importing plugin '%s':" % name
						__import__(name)
					else:
						print "[INIT]  Plugin '%s' was already imported." % name
					if name in sys.modules:
						module = sys.modules[name]
						for symbol in dir(module):
							if symbol.startswith('_') or symbol.endswith("Request"):
								continue
							type = getattr(module, symbol)
							try:
								if isinstance(type, types.ClassType) and issubclass(type, mamba.setup.Configuration):
									print "[INIT]  Custom setup=%s from %s" % (symbol, filename)
									conf = getattr(sys.modules[name], symbol)(self.inifile)
							except TypeError, e:
								pass
		mamba.setup.config(conf)
		signal.signal(signal.SIGTERM, self.server.on_stop)
		self.server.run()
		
	def stop(self):
		self.server.on_stop()
		self.tear_down_server()


class WorkerThread(threading.Thread, mamba.util.Logger):
	
	def __init__(self, server, task_queue, thread_name, number, params):
		threading.Thread.__init__(self)
		mamba.util.Logger.__init__(self, thread_name)    # Name used for logging id.
		self.server = server
		self.number = number
		self.params = params
		self._task_queue = task_queue
		self.setName(thread_name)
		
	def run(self):
		while 1:
			task = None
			try:
				task = self._task_queue.get()
				task.worker = self
				if isinstance(task, mamba.task.StopTask):
					self.debug("- Worker '%s' got a StopTask and quits main-loop." % self.getName())
					break
				if task.next:
					task.next()
				else:
					reply = mamba.http.HtmlError(task, 500, "Requst class '%s' has undefined 'next' action pointer." % task.__class__)
					self.err(reply.message)
					reply.send()
					
			except mamba.task.SyntaxError, e:
				self.err('Task "%s" caused an exception.' % type(e))
				self.err('Thread', self.name, 'threw an uncaught exception of type:', type(e))
				self.err('Error message:', e)
				self.err(traceback.format_exc())
				reply = mamba.http.HTTPErrorResponse(task, 400, 'Syntax error: %s' % str(e))
				reply.send()
			
			except mamba.task.PermissionError, e:
				self.err('Task "%s" caused an exception.' % type(e))
				self.err('Thread', self.name, 'threw an uncaught exception of type:', type(e))
				self.err('Error message:', e)
				self.err(traceback.format_exc())
				reply = mamba.http.HTTPErrorResponse(task, 401, 'Unauthorized: %s' % str(e))
				reply.send()
			
			except Exception, e:
				self.err('Task "%s" caused an exception.' % type(e))
				self.err('Thread', self.name, 'threw an uncaught exception of type:', type(e))
				self.err('Error message:', e)
				self.err(traceback.format_exc())
				reply = mamba.http.HTTPErrorResponse(task, 500, 'Server error: "%s"' % str(e))
				reply.send()
				
			except:
				self.err('Thread', self.name, 'threw an uncaught and undefined exception.')
				self.err('sys.exc_info() returned:', sys.exc_info())
				reply = mamba.http.HTTPErrorResponse(task, 500, 'Unknown server error.')
				reply.send()
						
			finally:
				if task:
					self.debug('Task done', task.__class__)
					self._task_queue.task_done()


class ThreadPool(mamba.util.Logger):
	
	def __init__(self, pool_name, server, queue_name, task_queue, threads):
		mamba.util.Logger.__init__(self, pool_name)
		self.server     = server
		self.queue_name = queue_name
		self.task_queue = task_queue
		params = self.get_ini_params()
		self.worker_threads = []
		for i in range(threads):
			number = i+1
			thread_name = str('%s_%s_%i' % (queue_name, pool_name, number))
			worker = WorkerThread(server, task_queue, thread_name, number, params)
			self.add_worker(worker)
			
	def get_ini_params(self):
		params = None
		for pool in mamba.setup.config().thread_pools[self.queue_name]:
			if pool.name == self.name:
				return pool.params
		return params
	
	def get_max_thread_id(self):
		biggest = 0
		for worker in self.worker_threads:
			biggest = max(biggest, worker.number)
		return biggest
	
	def get_number_of_restarts(self):
		return max(0, self.get_max_thread_id() - len(self.worker_threads))
		
	def add_worker(self, worker_thread):
		self.worker_threads.append(worker_thread)
		
	def bootup(self):
		self.debug('Thread-pool "%s" is booting up %i worker threads.' % (self.name, len(self.worker_threads)))
		for worker in self.worker_threads:
			worker.start()
			self.debug('- Worker thread "%s" started.' % worker.getName())
	
	def shutdown(self):
		self.debug('Thread-pool "%s" is stopping %i worker threads.' % (self.name, len(self.worker_threads)))
		for worker in self.worker_threads:
			stop = mamba.task.StopTask()
			self.task_queue.put(stop)
		
	def terminate(self):
		for worker in self.worker_threads:
			if worker != threading.currentThread():
				if worker.isAlive():
					#self.info('  joining on "%s"' % worker.getName())
					worker.join()
				#else:
					#self.info('  Worker "%s" has stopped already.' % worker.getName())
			else:
				self.err('Hey!!! I cannot wait on my self! Whats goin on here?')
			#self.info('  Worker thread "%s" finished.' % worker.getName())
		self.worker_threads = []
		
	def get_alive_threads(self):
		alive_threads = []
		for worker in self.worker_threads:
			if worker.isAlive():
				alive_threads.append(worker)
		return alive_threads
				
	def get_dead_threads(self):
		dead_threads = []
		for worker in self.worker_threads:
			if not worker.isAlive():
				dead_threads.append(worker)
		return dead_threads
		
	def renew_dead_threads(self):
		dead_threads = self.get_dead_threads()
		live_threads = self.get_alive_threads()
		#
		# Make sure dead threads are not running.
		#
		for dead in dead_threads:
			dead.join(0.0)
			del dead
		#
		# Replace all dead threads with identical new ones.
		# Enumerate from biggets thread number+1 and forward.
		#
		self.worker_threads = live_threads
		number = self.get_max_thread_id() + 1
		for dead in dead_threads:
			new_thread_name = str('%s_%s_%i' % (self.queue_name, self.name, number))
			new_thread = WorkerThread(self.server, self.task_queue, new_thread_name, number, self.get_ini_params())
			self.worker_threads.append(new_thread)
			new_thread.start()
			self.info("Replaced dead thread '%s' with '%s'." % (dead.getName(), new_thread.getName()))
			number += 1
