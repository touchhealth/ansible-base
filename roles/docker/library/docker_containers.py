#!/usr/bin/python -tt
# -*- coding: utf-8 -*-

import json
import hashlib
import httplib2
import os
import shutil
import tempfile
import traceback

def main():
	module = AnsibleModule(
		argument_spec = dict(
			state = dict(default = 'present', choices = ['present', 'prepared', 'absent']),
			containers = dict(required = True),
			required_restart = dict(required = False),
			remove_unused = dict(default = True),
			plan_file = dict(default = '/tmp/docker_containers_execution_plan')
		),
		supports_check_mode = True
	)

	params = module.params
	plan_file = params['plan_file']

	plan = []
	if os.path.exists(plan_file):
		existing_plan = load_plan(plan_file)
		config_hash = build_config_hash(params)

		if existing_plan['config_hash'] == config_hash:
			plan = existing_plan
		else:
			plan = build_plan(module, params)
			dump_plan(plan, plan_file)
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

def run_complex_command(module, cmd):
	if 'type' not in cmd:
		return 1, None, 'No type defined in complex command'

	cmd_type = cmd['type']
	args = cmd['args']

	if cmd_type == 'pull_image':
		return run_pull_image(module, args)

	if cmd_type == 'patch_image':
		return run_patch_image(module, args)

	if cmd_type == 'remove_images':
		return run_remove_images(module, args)

	if cmd_type == 'stop_container':
		return run_stop_container(module, args)

	if cmd_type == 'start_container':
		return run_start_container(module, args)

	return 1, None, 'Unknown command type: {0}'.format(cmd_type)

def run_pull_image(module, args):
	image = args['image']

	rc, out, err = module.run_command(['docker', 'pull', image])

	inspect_rc, _, _ = docker_inspect(module, "{{.Id}}", image)

	if inspect_rc == 0:
		return 0, None, None

	return rc, out, err

def run_start_container(module, args):
	cmd = args['cmd']

	# make sure there's not container with its name
	rc, out, err = run_stop_container(module, args)

	if rc != 0:
		return rc, out, err

	return module.run_command(cmd)

def run_stop_container(module, args):
	container_name = args['container_name']
	status, _, _ = inspect_container_state(module, container_name)

	stop_cmds = build_stop_container_cmds(container_name, status)

	for stop_cmd in stop_cmds:
		rc, out, err = run_command(module, stop_cmd)

		if rc != 0:
			return rc, out, err

	return 0, None, None

def run_patch_image(module, args):
	#   args = {
	#     image: localhost:5000/vedocs-elo:latest,
	#     patches: [
	#       { add: { host: x, image: y } },
	#       { run: echo 'viva' }
	#     ],
	#     result_image: vedocs-elo:latest_hash
	#   }
	image = args['image']
	patches = args['patches']
	result_image = args['result_image']
	temp_dir = tempfile.mkdtemp(prefix = 'tmp-docker-build-')

	dockerfile_path = os.path.join(temp_dir, 'Dockerfile')
	with open(dockerfile_path, 'w') as dockerfile:
		dockerfile.write('FROM {0}\n'.format(image))

		for patch in patches:
			if 'run' in patch:
				dockerfile.write('RUN {0}\n'.format(patch['run']))
			elif 'add' in patch:
				head, tail = os.path.split(patch['add']['host'])

				if not tail:
					head, tail = os.path.split(head)
				# copia dos arquivos realizada por comando nativo e flag -p para
				# manter os metadados e permitir a utilizacao dos caches do docker
				rc, out, err = module.run_command(['cp', '-rp', patch['add']['host'], temp_dir])
				if rc != 0:
					return rc, out, err

				dockerfile.write('ADD {0} {1}\n'.format(tail, patch['add']['image']))
	try:
		return module.run_command(
			['docker', 'build', '-t={0}'.format(result_image), temp_dir]
		)
	finally:
		shutil.rmtree(temp_dir)

def run_remove_images(module, args):
	#   args = dict(
	#		candidates_for_removal = [abc,def],
	#		used_image_names = [localhost:5000/solr-vedocs:latest, localhost:5000/vedocs:latest_12345]
	#	)
	candidates_for_removal = args['candidates_for_removal']
	used_image_names = args['used_image_names']

	used_image_ids = []
	for used_image_name in used_image_names:
		rc, out, err = docker_inspect(module, "{{.Id}}", used_image_name)
		if rc == 0:
			used_image_ids.append(out)

	unused_image_ids = [image_id for image_id in candidates_for_removal if image_id not in used_image_ids]

	for unused_image_id in unused_image_ids:
		rc, out, err = module.run_command(['docker', 'rmi', unused_image_id])

		# falhas em remocao de imagem serao ignoradas; pode acontecer da imagem 
		# estar na stack de algum container sendo executado
		
		#if rc != 0:
		#	return rc, out, err

	return 0, None, None

def run_command(module, cmd):
	rc, out, err = 0, None, None

	if not module.check_mode:
		if isinstance(cmd, basestring) or isinstance(cmd, list):
			rc, out, err = module.run_command(cmd)
		elif isinstance(cmd, dict):
			rc, out, err = run_complex_command(module, cmd)

	return rc, out, err

def execute_plan(module, plan, plan_file):
	executed = []
	failed_message = None

	cmds = plan['cmds']

	while cmds:
		cmd = cmds[0]

		rc, out, err = run_command(module, cmd)

		if rc == 0:
			cmds.pop(0)
			executed.append(cmd)
			dump_plan(plan, plan_file)
		else:
			failed_message = err
			break

	if not cmds:
		os.remove(plan_file)
		
	return executed, failed_message

def load_plan(plan_file):
	with open(plan_file, 'r') as plan_file:
		return json.load(plan_file)

def dump_plan(plan, plan_file):
	with open(plan_file, 'w') as plan_file:
		json.dump(plan, plan_file, indent=4, separators=(',', ': '))

def build_plan(module, params):
	state = params['state']
	containers = params['containers']
	required_restart = params['required_restart']
	remove_unused = params['remove_unused']

	config_hash = build_config_hash(params)

	dict_containers = build_dict_containers(containers)

	candidates_for_removal = get_candidates_for_removal(module)

	decide_containers_to_update(module, containers, dict_containers, required_restart, state)
	
	stop_cmds = plan_stop_containers(containers, dict_containers)

	prepare_cmds = plan_prepare_images(containers, dict_containers, state)

	start_cmds, used_image_names = plan_start_containers(containers, dict_containers, state)

	rmi_cmds = plan_remove_images(candidates_for_removal, used_image_names)

	cmds = []

	if stop_cmds or start_cmds or prepare_cmds:
		if state == 'prepared':
			cmds = prepare_cmds
		else:
			if boolean_value(remove_unused):
				cmds = prepare_cmds + stop_cmds + rmi_cmds + start_cmds
			else:
				cmds = prepare_cmds + stop_cmds + start_cmds

	return dict(
		config_hash = config_hash,
		cmds = cmds
	)

def build_config_hash(params):
	return json_hash(dict(
		state = params['state'],
		containers = [ normalize_container(c) for c in params['containers'] ]
	))

def decide_containers_to_update(module, containers, dict_containers, required_restart, state):
	inspect_containers_state(module, containers, dict_containers)
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
	if state == 'present' or state == 'prepared':
		if required_restart is not None and dict_container['name'] in required_restart and boolean_value(required_restart[dict_container['name']]):
			return True
		if dict_container['status'] == '' or dict_container['status'] == 'stopped':
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
		
		try:
			latest_commit = get_latest_commit(container, dict_container['image'])
		except Exception as e:
			latest_commit = docker_inspect_label(module, 'commitId', dict_container['image'])
		
		dict_container['latest_commit'] = latest_commit

def plan_prepare_images(containers, dict_containers, state):
	cmds = []
	
	if state == 'present' or state == 'prepared':
		for container in containers:
			container_name = container['name']
			dict_container = dict_containers[container_name]
			
			if dict_container['must_be_updated']:
				cmds.append(dict(
					type = 'pull_image',
					comment = 'Tarefa para garantir a existencia da imagem',
					args = dict(
						image = dict_container['image']
					)
				))
				if 'patches' in dict_container['container']:
					cmds.append(dict(
						type = 'patch_image',
						comment = 'Tarefa para executar build de patches nas imagens docker',
						args = dict(
							image = dict_container['image'],
							patches = dict_container['container']['patches'],
							result_image = get_patched_image_name(dict_container)
						)
					))
	return cmds

def plan_start_containers(containers, dict_containers, state):
	cmds = []
	used_image_names = []
	
	if state == 'present':
		for container in containers:
			container_name = container['name']
			dict_container = dict_containers[container_name]

			if dict_container['must_be_updated']:
				cmd, used_image_name = plan_start_container(dict_container)
				cmds.append(cmd)
				used_image_names.append(used_image_name)
	
	return cmds, used_image_names

# a imagem com patch sera montada sem endereco do registro, com o nome original,
# e a tag sera a tag original + a hash dos patches; caso algum arquivo do patch
# seja diferente, o build do proprio docker devera provocar a renomeacao da imagem 
# nova para uma tag previamente existente
def get_patched_image_name(dict_container):
	container = dict_container['container']
	tag = '{0}_{1}'.format(container['tag'], json_hash(container['patches']))
	return '{0}:{1}'.format(container['image'], tag)

def plan_stop_containers(containers, dict_containers):
	cmds = []
	
	for container in reversed(containers):
		container_name = container['name']
		dict_container = dict_containers[container_name]

		if dict_container['must_be_updated']:
			cmds += plan_stop_container(container_name)

	return cmds

def plan_stop_container(container_name):
	cmds = []

	cmds.append(
		dict(
			type = 'stop_container',
			comment = 'Para e remove um container existente',
			args = dict(
				container_name = container_name
			)
		)
	)

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

		if current_commit == '':
			image_id = docker_inspect(module, '{{.Image}}', container_name)[1]
			
			current_commit = docker_inspect_label(module, 'commitId', image_id)
		
		current_config_hash = docker_inspect_label(module, 'configHash', container_name)
	
	return status, current_commit, current_config_hash

def get_latest_commit(container, image):
	if 'registry' in container:
		h = httplib2.Http()

		tag = container['tag'] if 'tag' in container else 'latest'
		url = "http://{0}/v2/{1}/manifests/{2}".format(container['registry'], container['image'], tag)

		try:
			headers, content = h.request(url, "GET")
			manifest = json.loads(content)
			data = json.loads(manifest['history'][0]['v1Compatibility'])
		
			config = data['config']
			labels_key = 'Labels'
			commit_id_key = 'commitId'

			if labels_key in config and commit_id_key in config[labels_key]:
				return config[labels_key][commit_id_key]
			return ''
		except Exception as e:
			raise Exception('Erro tentando acessar ' + url + '\n' + traceback.format_exc())

	return docker_inspect_label(module, 'commitId', image)

def get_candidates_for_removal(module):
	image_ids = get_image_ids(module)
	
	used_image_ids = get_used_image_ids(module)

	candidates_for_removal = [item for item in image_ids if item not in used_image_ids]

	return candidates_for_removal

def get_image_ids(module):
	rc, out, err = module.run_command(['docker', 'images', '-q', '--no-trunc'])

	image_ids = [item for item in out.split('\n') if item]

	return image_ids

def get_used_image_ids(module):
	rc, out, err = module.run_command(['docker', 'ps', '-a', '-q'])

	container_ids = [item for item in out.split('\n') if item]
	used_image_ids = [docker_inspect(module, '{{.Image}}', item)[1] for item in container_ids]

	return used_image_ids

def plan_remove_images(candidates_for_removal, used_image_names):
	cmds = []

	cmds.append(dict(
		type = 'remove_images',
		comment = 'Tarefa para remover as imagens que nao serao mais utilizadas',
		args = dict(
			candidates_for_removal = candidates_for_removal,
			used_image_names = used_image_names
		)
	))

	return cmds

def build_dict_containers(containers):
	dict_containers = dict()
	
	for container in containers:
		n_container = normalize_container(container)
		
		if 'registry' in container:
			if 'tag' in container:
				image = '{0}/{1}:{2}'.format(container['registry'], container['image'], container['tag'])
			else:
				image = '{0}/{1}'.format(container['registry'], container['image'])
		else:
			if 'tag' in container:
				image = '{1}:{2}'.format(container['image'], container['tag'])
			else:
				image = container['image']

		dict_container = dict(
			name = n_container['name'],
			image = image,
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
	attr_as_is = ['name', 'daemon', 'registry', 'image', 'tag', 'environment_variables', 'patches', 'args', 'extra_options']
	n_container = dict([(key, container[key]) for key in attr_as_is if key in container])
	
	normalize_volumes(n_container, container)
	normalize_ports(n_container, container)
	normalize_links(n_container, container)
	normalize_volumes_from(n_container, container)

	return n_container

def normalize_volumes(n_container, container):
	normalize_list_of_dicts(n_container, container, 'volumes', ['container', 'host', 'mode'], 'container')

def normalize_ports(n_container, container):
	normalize_list_of_dicts(n_container, container, 'ports', ['container', 'host', 'ip', 'protocol'], 'container')

def normalize_links(n_container, container):
	normalize_list_of_dicts(n_container, container, 'links', ['alias', 'name'], 'alias')

def normalize_volumes_from(n_container, container):
	if 'volumes_from' in container:
		n_container['volumes_from'] = sorted(container['volumes_from'])
	else:
		n_container['volumes_from'] = []

def normalize_list_of_dicts(n_container, container, name, keys, sort_key):
	n_list = []
	
	if name in container:		
		for item in container[name]:
			n_item = dict([(key, item[key]) for key in keys if key in item])
			n_list.append(n_item)
		
		n_list.sort(key = lambda i: i[sort_key])
	
	n_container[name] = n_list

def plan_start_container(dict_container):
	container = dict_container['container']
	
	cmd = ['docker', 'run', '--name', container['name']]
	
	cmd += ['--label', '{0}={1}'.format('configHash', dict_container['latest_config_hash'])]
	
	if 'daemon' in container and container['daemon']:
		cmd += ['-d', '--restart', 'always']
	
	if 'ports' in container:
		for port in container['ports']:
			# dummy, depois ver como melhorar
			if 'protocol' in port:
				if 'ip' in port:
					cmd += ['-p', '{0}:{1}:{2}/{3}'.format(port['ip'], port['host'], port['container'], port['protocol'])]
				else:
					cmd += ['-p', '{0}:{1}/{2}'.format(port['host'], port['container'], port['protocol'])]
			else:
				if 'ip' in port:
					cmd += ['-p', '{0}:{1}:{2}'.format(port['ip'], port['host'], port['container'])]
				else:
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

	if 'extra_options' in container:
		extra_options = container['extra_options']
		if isinstance(extra_options, basestring):
			cmd += [extra_options]
		elif isinstance(extra_options, list):
			cmd += extra_options

	if 'patches' in container:
		image = get_patched_image_name(dict_container)
	else:
		image = dict_container['image']

	cmd += [image]
	
	if 'args' in container:
		args = container['args']
		if isinstance(args, basestring):
			cmd += [args]
		elif isinstance(args, list):
			cmd += args

	complex_command = dict(
		type = 'start_container',
		comment = 'Tarefa para iniciar o container; ela pode provocar tambem a remocao de algum container de mesmo nome preexistente',
		args = dict(
			cmd = cmd,
			container_name = container['name']
		)
	)

	return complex_command, image

def build_stop_container_cmds(container_name, status):
	cmds = []

	if status == 'running':
		cmds.append([ 'docker', 'stop', container_name ])
	if status != '':
		cmds.append([ 'docker', 'rm', '-fv', container_name ])

	return cmds

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

def json_hash(obj):
	return md5hash(json.dumps(obj, sort_keys=True, separators=(',',':')))

def md5hash(string):
    m = hashlib.md5()
    m.update(string.encode('utf-8'))
    return m.hexdigest()

from ansible.module_utils.basic import *
main()
