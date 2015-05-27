#!/usr/bin/python

import json
import hashlib
import httplib2

def main():
	module = AnsibleModule(
		argument_spec = dict(
			state = dict(default = 'present', choices = ['present', 'absent']),
			containers = dict(required = True),
			required_restart = dict(required = False),
			plan_file = dict(default = '/tmp/docker_containers_execution_plan')
		),
		supports_check_mode = True
	)

	params = module.params
	plan_file = params['plan_file']

	plan = []
	if os.path.exists(plan_file):
		plan = load_plan(plan_file)
	else:
		plan = build_plan(module, params)
		dump_plan(plan, plan_file)

	executed, failed_message = execute_plan(module, plan, plan_file)

	if failed_message is not None:
		module.fail_json(
			msg = failed_message,
			executed = executed,
			plan = plan
		)
	else:
		module.exit_json(
			changed = len(executed) != 0,
			executed = executed
		)

def execute_plan(module, plan, plan_file):
	executed = []
	failed_message = None

	while plan:
		cmd = plan.pop(0)

		rc, out, err = 0, '', ''

		if not module.check_mode:
			rc, out, err = module.run_command(cmd)

		if rc == 0:
			executed.append(cmd)
			dump_plan(plan, plan_file)
		else:
			failed_message = err
			break

	if not plan:
		os.remove(plan_file)
		
	return executed, failed_message

def load_plan(plan_file):
	with open(plan_file, 'r') as plan_file:
		return json.load(plan_file)

def dump_plan(plan, plan_file):
	with open(plan_file, 'w') as plan_file:
		json.dump(plan, plan_file)

def build_plan(module, params):
	state = params['state']
	containers = params['containers']
	required_restart = params['required_restart']

	dict_containers = build_dict_containers(containers)
	unused_image_ids = get_unused_image_ids(module)
	inspect_containers_state(module, containers, dict_containers)
	decide_containers_to_update(containers, dict_containers, required_restart, state)
	
	stop_and_remove_cmds = stop_and_remove_containers(containers, dict_containers)
	pull_and_start_cmds = pull_images_start_containers(containers, dict_containers, state)
	rmi_cmds = remove_images(unused_image_ids)

	if stop_and_remove_cmds or pull_and_start_cmds:
		return stop_and_remove_cmds + pull_and_start_cmds + rmi_cmds
	else:
		return []

def decide_containers_to_update(containers, dict_containers, required_restart, state):	
	for container in containers:
		container_name = container['name']
		dict_container = dict_containers[container_name]
		
		if should_update(dict_container, required_restart, state):
			mark_to_update(dict_container)

def mark_to_update(dict_container):
	dict_container['must_be_updated'] = True
	
	for dependent in dict_container['required_by']:
		mark_to_update(dependent)

def boolean_value(value):
	if isinstance(value, bool):
		return value
	if isinstance(value, str):
		return value.lower() in ['true', '1', 't', 'y', 'yes']
	raise Exception('Failed to parse boolean value from ' + value)

def should_update(dict_container, required_restart, state):
	if state == 'present':
		if dict_container['name'] in required_restart and boolean_value(required_restart[dict_container['name']]):
			return True
		if dict_container['status'] == '':
			return True
		if dict_container['current_commit'] != dict_container['latest_commit']:
			return True
		if dict_container['current_config_hash'] != dict_container['latest_config_hash']:
			return True
	else:
		if dict_container['status'] != '':
			return True
	
	return False

def inspect_containers_state(module, containers, dict_containers):
	for container in containers:
		container_name = container['name']
		dict_container = dict_containers[container_name]
		
		status, current_commit, current_config_hash = inspect_container_state(module, container['name'])
		
		dict_container['status'] = status
		dict_container['current_commit'] = current_commit
		dict_container['current_config_hash'] = current_config_hash
		
		latest_commit = get_latest_commit(container['registry'], container['image'], container['tag'])
		
		dict_container['latest_commit'] = latest_commit

def pull_images_start_containers(containers, dict_containers, state):
	cmds = []
	
	if state == 'present':
		for container in containers:
			container_name = container['name']
			dict_container = dict_containers[container_name]
		
			if dict_container['must_be_updated']:
				cmds.append(['docker', 'pull', dict_container['image']])
				cmds.append(build_docker_run(dict_container))
	
	return cmds

def stop_and_remove_containers(containers, dict_containers):
	cmds = []
	
	for container in reversed(containers):
		container_name = container['name']
		dict_container = dict_containers[container_name]
		
		if dict_container['must_be_updated']:
			status = dict_container['status']
			
			if status == 'running':
				cmds.append([ 'docker', 'stop', container_name ])
			if status != '':
				cmds.append([ 'docker', 'rm', '-fv', container_name ])
				
	return cmds

def inspect_container_state(module, container_name):
	status, current_commit, current_config_hash = ('', '', '')
	
	rc, out, err = docker_inspect(module, '{{.State.Running}}', container_name)
	
	# existing container...
	if rc == 0:
		if out == 'true':
			status = 'running'
		else:
			status = 'stopped'
		
		current_commit = docker_inspect_label(module, 'commitId', container_name)
		
		current_config_hash = docker_inspect_label(module, 'configHash', container_name)
	
	return status, current_commit, current_config_hash

def get_latest_commit(registry, image, tag):
	h = httplib2.Http(".cache")
	headers, content = h.request("http://{0}/v2/{1}/manifests/{2}".format(registry, image, tag), "GET")
	manifest = json.loads(content)
	data = json.loads(manifest['history'][0]['v1Compatibility'])
	return data['config']['Labels']['commitId']

def get_image_ids(module):
	rc, out, err = module.run_command(['docker', 'images', '-q'])

	image_short_ids = [item for item in out.split('\n') if item]
	image_ids = [docker_inspect(module, '{{.Id}}', item)[1] for item in image_short_ids]

	return image_ids

def get_used_image_ids(module):
	rc, out, err = module.run_command(['docker', 'ps', '-a', '-q'])

	container_ids = [item for item in out.split('\n') if item]
	used_image_ids = [docker_inspect(module, '{{.Image}}', item)[1] for item in container_ids]

	return used_image_ids

def get_unused_image_ids(module):
	image_ids = get_image_ids(module)
	
	used_image_ids = get_used_image_ids(module)

	unused_images_ids = [item for item in image_ids if item not in used_image_ids]

	return unused_images_ids

def remove_images(images):
	cmds = []

	for image in images:
		cmds.append(['docker', 'rmi', image])
		
	return cmds

def build_dict_containers(containers):
	dict_containers = dict()
	
	for container in containers:
		n_container = normalize_container(container)
		dict_container = dict(
			name = n_container['name'],
			image = '{0}/{1}:{2}'.format(container['registry'], container['image'], container['tag']),
			container = n_container,
			required_by = [],
			latest_config_hash = json_hash(n_container),
			must_be_updated = False
		)
		
		dict_containers[container['name']] = dict_container
	
	for container in containers:
		dict_container = dict_containers[container['name']]
	
		if 'volumes_from' in container:
			volumes_from = container['volumes_from']
			for vol_provider in volumes_from:
				dict_containers[vol_provider]['required_by'].append(dict_container)
	
		if 'links' in container:
			links = container['links']
			for link in links:
				dict_containers[link['name']]['required_by'].append(dict_container)
	
	return dict_containers

def normalize_container(container):
	attr_as_is = ['name', 'daemon', 'registry', 'image', 'tag', 'environment_variables']
	n_container = dict([(key, container[key]) for key in attr_as_is if key in container])
	
	normalize_volumes(n_container, container)
	normalize_ports(n_container, container)
	normalize_links(n_container, container)
	normalize_volumes_from(n_container, container)

	return n_container

def normalize_volumes_from(n_container, container):
	if 'volumes_from' in container:
		n_container['volumes_from'] = sorted(container['volumes_from'])
	else:
		n_container['volumes_from'] = []

def normalize_links(n_container, container):
	normalize_list_of_dicts(n_container, container, 'links', ['alias', 'name'], 'alias')

def normalize_volumes(n_container, container):
	normalize_list_of_dicts(n_container, container, 'volumes', ['container', 'host', 'mode'], 'container')

def normalize_ports(n_container, container):
	normalize_list_of_dicts(n_container, container, 'ports', ['container', 'host'], 'container')

def normalize_list_of_dicts(n_container, container, name, keys, sort_key):
	n_list = []
	
	if name in container:		
		for item in container[name]:
			n_item = dict([(key, item[key]) for key in keys if key in item])
			n_list.append(n_item)
		
		n_list.sort(key = lambda i: i[sort_key])
	
	n_container[name] = n_list

def build_docker_run(dict_container):
	container = dict_container['container']
	
	cmd = ['docker', 'run', '--name', container['name']]
	
	cmd += ['--label', '{0}={1}'.format('configHash', dict_container['latest_config_hash'])]
	
	cmd += ['--restart', 'always']
	
	if 'daemon' in container and container['daemon']:
		cmd += ['-d']
	
	if 'ports' in container:
		for port in container['ports']:
			cmd += ['-p', '{0}:{1}'.format(port['host'], port['container'])]
	
	if 'links' in container:
		for link in container['links']:
			cmd += ['--link', '{0}:{1}'.format(link['name'], link['alias'])]
	
	if 'volumes' in container:
		for volume in container['volumes']:
			if 'mode' in volume:
				cmd += ['-v', '{0}:{1}:{2}'.format(volume['host'], volume['container'], volume['mode'])]
			else:
				cmd += ['-v', '{0}:{1}'.format(volume['host'], volume['container'])]
	
	if 'volumes_from' in container:
		for vol_provider in container['volumes_from']:
			cmd += ['--volumes-from', vol_provider]
	
	if 'environment_variables' in container:
		variables = container['environment_variables']
		for key in variables:
			cmd += ['-e', '{0}={1}'.format(key, variables[key])]

	cmd += [dict_container['image']]
	
	return cmd

def docker_inspect_label(module, label_name, name):
	label = ''
	
	rc, out, err = docker_inspect(module, '{{' + '.Config.Labels.{0}'.format(label_name) + '}}', name)
	if rc == 0 and out != '<no value>':
		label = out
	
	return label

def docker_inspect(module, path, name):
	cmd = ['docker', 'inspect' ,'-f', path, name]
	
	rc, out, err = module.run_command(cmd)
	out = out.strip()
	
	return rc, out, err

def json_hash(container):
	return md5hash(json.dumps(container, sort_keys=True, separators=(',',':')))

def md5hash(string):
    m = hashlib.md5()
    m.update(string.encode('utf-8'))
    return m.hexdigest()

from ansible.module_utils.basic import *
main()