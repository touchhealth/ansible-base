#!/usr/bin/python

import httplib2
import urllib
import time
import traceback

def main():
	module = AnsibleModule(
		argument_spec = dict(
			base_url = dict(required = True),
			path = dict(default = ''),
			login_path = dict(default = ''),
			login_method = dict(default = 'POST'),
			login_data = dict(required = False),
			login_retries = dict(default = 120),
			login_interval = dict(default = 10)
		),
		supports_check_mode = True
	)

	params = module.params

	base_url = params['base_url']
	path = params['path']

	login_path = params['login_path']
	login_method = params['login_method']
	login_data = params['login_data']

	login_retries = params['login_retries']
	login_interval = params['login_interval']

	cookie = login(
		module,
		base_url + login_path,
		login_method,
		login_data,
		login_retries,
		login_interval
	)

	response, content = invoke_url(module, base_url + path, cookie)

	if response['status'] == '500':
		module.fail_json(msg = content)
	else:
		module.exit_json(changed = response['status'] == '201', ok = True, msg = content)

class FailedLoginException(Exception):
	def __init__(self, response):
		self.response = response
	def __str__(self):
		return str(self.response)

def login(module, url, login_method, login_data, login_retries, login_interval):
	i = login_retries
	last_exception = None

	while i > 0:
		h = httplib2.Http()
		
		try:
			headers = dict()
			body = dict()

			if login_method == 'POST':
				headers['Content-type'] = 'application/x-www-form-urlencoded'

				if login_data:
					body = login_data

			response, content = h.request(
				url,
				login_method,
				headers = headers,
				body = urllib.urlencode(body)
			)

			
			if not response['status'] or response['status'][0] != '2':
				raise FailedLoginException(response)

			return response['set-cookie']
		except FailedLoginException as e:
			last_exception = e
			break
		except Exception as e:
			last_exception = e
			time.sleep(login_interval)
			i -= 1

	module.fail_json(msg = 'Failed login: ' + url + '\n' + traceback.format_exc())

def invoke_url(module, url, cookie):
	h = httplib2.Http()
	
	try:
		response, content = h.request(url, 'POST', headers = dict(Cookie = cookie))

		return response, content
	except Exception as e:
		raise Exception('Failed connection: ' + url + '\n' + traceback.format_exc())

from ansible.module_utils.basic import *
main()