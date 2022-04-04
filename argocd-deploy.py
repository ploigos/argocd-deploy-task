import os
import sh
import sys
import re
from io import StringIO
import yaml
from pathlib import Path


GIT_REPO_REGEX = re.compile(r"(?P<protocol>^https:\/\/|^http:\/\/)?(?P<address>.*$)")
ARGOCD_OP_IN_PROGRESS_REGEX = re.compile(
    r'.*FailedPrecondition.*another\s+operation\s+is\s+already\s+in\s+progress',
    re.DOTALL
)
ARGOCD_HEALTH_STATE_TRANSITIONED_FROM_HEALTHY_TO_DEGRADED = re.compile(
    r".*level=fatal.*health\s+state\s+has\s+transitioned\s+from\s.+\s+to\s+Degraded",
    re.DOTALL
)
MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES = 2
MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_HEALTH_RETRIES = 2


def create_working_dir_sub_dir(work_dir_path, sub_dir_relative_path=""):
    """Create a folder under the working/stepname folder.

    Returns
    -------
    str
        Path to created working sub directory.
    """
    file_path = os.path.join(work_dir_path, sub_dir_relative_path)
    os.makedirs(file_path, exist_ok=True)
    return file_path


def clone_repo( # pylint: disable=too-many-arguments
    repo_dir,
    repo_url,
    repo_branch,
    git_email,
    git_name,
    username=None,
    password=None
):
    """Clones and checks out the deployment configuration repository.

    Parameters
    ----------
    repo_dir : str
        Path to where to clone the repository
    repo_uri : str
        URI of the repository to clone.
    git_email : str
        email to use when performing git operations in the cloned repository
    git_name : str
        name to use when performing git operations in the cloned repository

    Returns
    -------
    str
        Path to the directory where the deployment configuration repository was cloned
        and checked out.

    Raises
    ------
    StepRunnerException
    * if error cloning repository
    * if error checking out branch of repository
    * if error configuring repo user
    """
    repo_match = GIT_REPO_REGEX.match(repo_url)
    repo_protocol = repo_match.groupdict()['protocol']
    repo_address = repo_match.groupdict()['address']
    # if deployment config repo uses http/https push using user/pass
    # else push using ssh
    if username and password and repo_protocol and re.match(
            r'^http://|^https://',
            repo_protocol
    ):
        repo_url_with_auth = \
            f"{repo_protocol}{username}:{password}" \
            f"@{repo_address}"
    else:
        repo_url_with_auth = repo_url
    try:
        sh.git.clone( # pylint: disable=no-member
            repo_url_with_auth,
            repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise f"Error cloning repository ({repo_url}): {error}"

    try:
        # no atomic way in git to checkout out new or existing branch,
        # so first try to check out existing, if that doesn't work try new
        try:
            sh.git.checkout(  # pylint: disable=no-member
                repo_branch,
                _cwd=repo_dir,
                _out=sys.stdout,
                _err=sys.stderr
            )
        except sh.ErrorReturnCode:
            sh.git.checkout(
                '-b',
                repo_branch,
                _cwd=repo_dir,
                _out=sys.stdout,
                _err=sys.stderr
            )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise f"Unexpected error checking out new or existing branch ({repo_branch}) from repository ({repo_url}): {error}"

    try:
        sh.git.config( # pylint: disable=no-member
            'user.email',
            git_email,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
        sh.git.config( # pylint: disable=no-member
            'user.name',
            git_name,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise (
            f"Unexpected error configuring git user.email ({git_email})"
            f" and user.name ({git_name}) for repository ({repo_url})"
            f" in directory ({repo_dir}): {error}"
        )

    return repo_dir


def write_working_file(work_dir_path, filename, contents=None):
    """Write content or touch filename in working directory for this step.

    Parameters
    ----------
    filename : str
        File name to create
    contents : str, optional
        Contents to write to the file

    Returns
    -------
    str
        Return a string to the file path
    """
    # eg: step-runner-working/step_name
    file_path = os.path.join(work_dir_path, filename)

    # sub-directories might be passed filename, eg: foo/filename
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if contents is None:
        Path(file_path).touch()
    else:
        with open(file_path, 'wb') as file:
            file.write(contents)
    return file_path


def _update_yaml_file_value(work_dir_path, file, yq_path, value):
    """Update a YAML file using YQ.

    Parameters
    ----------
    file : str
        Path to file to update.
    yq_path : str
        YQ path to the value to update.
    value: str
        value to update the `yq_path`

    Returns
    -------
    str
        Path to the file to update.

    Raises
    ------
    StepRunnerException
        If error updating file.
    """
    # NOTE: use a YQ script to update so that comment can be injected
    yq_script_file = write_working_file(
        work_dir_path= work_dir_path,
        filename='update-yaml-file-yq-script.yaml',
        contents=bytes(
            f"- command: update\n"
            f"  path: {yq_path}\n"
            f"  value:\n"
            f"    {value} # written by ploigos-step-runner\n",
            'utf-8'
        )
    )

    # inplace update the file
    try:
        sh.yq.write( # pylint: disable=no-member
            file,
            f'--script={yq_script_file}',
            '--inplace'
        )
    except sh.ErrorReturnCode as error:
        raise (
            f"Error updating YAML file ({file}) target ({yq_path}) with value ({value}):"
            f" {error}"
        )

    return file


def _git_commit_file(git_commit_message, file_path, repo_dir):
    try:
        sh.git.add( # pylint: disable=no-member
            file_path,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise (
            f"Unexpected error adding file ({file_path}) to commit"
            f" in git repository ({repo_dir}): {error}"
        )

    try:
        sh.git.commit( # pylint: disable=no-member
            '--allow-empty',
            '--all',
            '--message', git_commit_message,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise (
            f"Unexpected error commiting file ({file_path})"
            f" in git repository ({repo_dir}): {error}"
        )


def _git_push(repo_dir, tag, url=None):
    """
    Raises string if error pushing commits
    """

    git_push = sh.git.push.bake(url) if url else sh.git.push

    # push commits
    try:
        git_push(
            _cwd=repo_dir,
            _out=sys.stdout
        )
    except sh.ErrorReturnCode as error:
        raise (
            f"Error pushing commits from repository directory ({repo_dir}) to"
            f" repository ({url}): {error}"
        )


def _git_push_deployment_config_repo(
        deployment_config_repo,
        deployment_config_repo_dir,
        username,
        password
):
    deployment_config_repo_match = GIT_REPO_REGEX.match(deployment_config_repo)
    deployment_config_repo_protocol = deployment_config_repo_match.groupdict()['protocol']
    deployment_config_repo_address = deployment_config_repo_match.groupdict()['address']

    # if deployment config repo uses http/https push using user/pass
    # else push using ssh
    if deployment_config_repo_protocol and re.match(
            r'^http://|^https://',
            deployment_config_repo_protocol
    ):
        deployment_config_repo_with_user_pass = \
            f"{deployment_config_repo_protocol}{username}:{password}" \
            f"@{deployment_config_repo_address}"
        _git_push(
            repo_dir=deployment_config_repo_dir,
            url=deployment_config_repo_with_user_pass
        )
    else:
        _git_push(
            repo_dir=deployment_config_repo_dir
        )


def _argocd_sign_in(
        argocd_api,
        username,
        password,
        insecure=False
):
    """Signs into ArgoCD CLI.

    Raises
    ------
    StepRunnerException
        If error signing into ArgoCD CLI.
    """
    try:
        insecure_flag = None
        if insecure:
            insecure_flag = '--insecure'

        sh.argocd.login(  # pylint: disable=no-member
            argocd_api,
            f'--username={username}',
            f'--password={password}',
            insecure_flag,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise f"Error logging in to ArgoCD: {error}"


def _argocd_app_create_or_update( # pylint: disable=too-many-arguments
        argocd_app_name,
        project,
        repo,
        revision,
        path,
        dest_server,
        dest_namespace,
        auto_sync,
        values_files
):
    """Creates or updates an ArgoCD App.

    Raises
    ------
    StepRunnerException
        If error creating or updating ArgoCD app.
    """
    try:
        if str(auto_sync).lower() == 'true':
            sync_policy = 'automated'
        else:
            sync_policy = 'none'

        values_params = None
        if values_files:
            values_params = []
            for value_file in values_files:
                values_params += [f'--values={value_file}']

        sh.argocd.app.create(  # pylint: disable=no-member
            argocd_app_name,
            f'--repo={repo}',
            f'--revision={revision}',
            f'--path={path}',
            f'--dest-server={dest_server}',
            f'--dest-namespace={dest_namespace}',
            f'--sync-policy={sync_policy}',
            f'--project={project}',
            values_params,
            '--upsert',
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise f"Error creating or updating ArgoCD app ({argocd_app_name}): {error}"


def _argocd_app_wait_for_operation(argocd_app_name, argocd_timeout_seconds):
    """Waits for an existing operation on an ArgoCD Application to finish.

    Parameters
    ----------
    argocd_app_name : str
        Name of ArgoCD Application to wait for existing operations on.
    argocd_timeout_seconds : int
        Number of sections to wait before timing out waiting for existing operations to finish.

    Raises
    ------
    StepRunnerException
        If error (including timeout) waiting for existing ArgoCD Application operation to finish
    """
    try:
        print(
            f"Wait for existing ArgoCD operations on Application ({argocd_app_name})"
        )
        sh.argocd.app.wait( # pylint: disable=no-member
            argocd_app_name,
            '--operation',
            '--timeout', argocd_timeout_seconds,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise (
            f"Error waiting for existing ArgoCD operations on Application ({argocd_app_name})"
            f": {error}"
        )


def create_sh_redirect_to_multiple_streams_fn_callback(streams):
    """Creates and returns a function callback that will write given data to multiple given streams.

    AKA: this essentially allows you to do 'tee' for sh commands.

    Parameters
    ----------
    streams : list of io.IOBase
        Streams to write to.

    Examples
    --------
    Will write output directed at stdout to stdout and a results file and output directed
    at stderr to stderr and a results file.
    >>> with open('/tmp/results_file', 'w') as results_file:
    ...     out_callback = create_sh_redirect_to_multiple_streams_fn_callback([
    ...         sys.stdout,
    ...         results_file
    ...     ])
    ...     err_callback = create_sh_redirect_to_multiple_streams_fn_callback([
    ...         sys.stderr,
    ...         results_file
    ...     ])
    ...     sh.echo('hello world')
    hello world

    Returns
    -------
    function(data)
        Function that takes one parameter, data, and writes that value to all the given streams.
    """

    def sh_redirect_to_multiple_streams(data):
        for stream in streams:
            stream.write(data)
            stream.flush()

    return sh_redirect_to_multiple_streams


def _argocd_app_wait_for_health(argocd_app_name, argocd_timeout_seconds):
    """Waits for ArgoCD Application to reach Healthy state.

    Parameters
    ----------
    argocd_app_name : str
        Name of ArgoCD Application to wait for Healthy state of.
    argocd_timeout_seconds : int
        Number of sections to wait before timing out waiting for Healthy state.

    Raises
    ------
    StepRunnerException
        If error (including timeout) waiting for existing ArgoCD Application Healthy state.
        If ArgoCD Application transitions from Healthy to Degraded while waiting for Healthy
        state.
    """
    for wait_for_health_retry in range(MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES):
        argocd_output_buff = StringIO()
        try:
            print(f"Wait for Healthy ArgoCD Application ({argocd_app_name}")
            out_callback = create_sh_redirect_to_multiple_streams_fn_callback([
                sys.stdout,
                argocd_output_buff
            ])
            err_callback = create_sh_redirect_to_multiple_streams_fn_callback([
                sys.stderr,
                argocd_output_buff
            ])
            sh.argocd.app.wait(  # pylint: disable=no-member
                argocd_app_name,
                '--health',
                '--timeout', argocd_timeout_seconds,
                _out=out_callback,
                _err=err_callback
            )
            break
        except sh.ErrorReturnCode as error:
            # if error waiting for Healthy state because entered Degraded state
            # while waiting for Healthy state
            # try again to wait for Healthy state assuming that on next attempt the
            # new degradation of Health will resolve itself.
            #
            # NOTE: this can happen based on bad timing if for instance an
            #       HorizontalPodAutoscaller doesn't enter Degraded state until after we are
            #       already waiting for the ArgoCD Application to enter Healthy state,
            #       but then the HorizontalPodAutoscaller will, after a time, become Healthy.
            if re.match(
                    ARGOCD_HEALTH_STATE_TRANSITIONED_FROM_HEALTHY_TO_DEGRADED,
                    argocd_output_buff.getvalue()
            ):
                print(
                    f"ArgoCD Application ({argocd_app_name}) entered Degraded state"
                    " while waiting for it to enter Healthy state."
                    f" Try ({wait_for_health_retry} out of"
                    f" {MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES}) again to"
                    " wait for Healthy state."
                )
            else:
                raise f"Error waiting for Healthy ArgoCD Application ({argocd_app_name}): {error}"


def _argocd_app_sync(
        argocd_app_name,
        argocd_sync_timeout_seconds,
        argocd_sync_retry_limit,
        argocd_sync_prune=True
): # pylint: disable=line-too-long
    # add any additional flags
    argocd_sync_additional_flags = []
    if argocd_sync_prune:
        argocd_sync_additional_flags.append('--prune')

    for wait_for_op_retry in range(MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES):
        # wait for any existing operations before requesting new synchronization
        #
        # NOTE: attempted work around for 'FailedPrecondition desc = another operation is
        #       already in progress' error
        # SEE: https://github.com/argoproj/argo-cd/issues/4505
        _argocd_app_wait_for_operation(
            argocd_app_name=argocd_app_name,
            argocd_timeout_seconds=argocd_sync_timeout_seconds
        )

        # sync app
        argocd_output_buff = StringIO()
        try:
            print(f"Request synchronization of ArgoCD app ({argocd_app_name}).")
            out_callback = create_sh_redirect_to_multiple_streams_fn_callback([
                sys.stdout,
                argocd_output_buff
            ])
            err_callback = create_sh_redirect_to_multiple_streams_fn_callback([
                sys.stderr,
                argocd_output_buff
            ])

            sh.argocd.app.sync(  # pylint: disable=no-member
                *argocd_sync_additional_flags,
                '--timeout', argocd_sync_timeout_seconds,
                '--retry-limit', argocd_sync_retry_limit,
                argocd_app_name,
                _out=out_callback,
                _err=err_callback
            )

            break
        except sh.ErrorReturnCode as error:
            # if error syncing because of in progress op
            # try again to wait for in progress op and do sync again
            #
            # NOTE: this can happen if we do the wait for op, and then an op starts and then
            #       we try to do a sync
            #
            # SEE: https://github.com/argoproj/argo-cd/issues/4505
            if re.match(ARGOCD_OP_IN_PROGRESS_REGEX, argocd_output_buff.getvalue()):
                print(
                    f"ArgoCD Application ({argocd_app_name}) has an existing operation"
                    " that started after we already waited for existing operations but"
                    " before we tried to do a sync."
                    f" Try ({wait_for_op_retry} out of"
                    f" {MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES}) again to"
                    " wait for the operation"
                )
                continue

            if not argocd_sync_prune:
                prune_warning = ". Sync 'prune' option is disabled." \
                                " If sync error (see logs) was due to resource(s) that need to be pruned," \
                                " and the pruneable resources are intentionally there then see the ArgoCD" \
                                " documentation for instructions for argo to ignore the resource(s)." \
                                " See: https://argoproj.github.io/argo-cd/user-guide/sync-options/#no-prune-resources" \
                                " and https://argoproj.github.io/argo-cd/user-guide/compare-options/#ignoring-resources-that-are-extraneous"
            else:
                prune_warning = ""

            raise (
                f"Error synchronization ArgoCD Application ({argocd_app_name})"
                f"{prune_warning}: {error}"
            )

    # wait for sync to finish
    _argocd_app_wait_for_health(
        argocd_app_name=argocd_app_name,
        argocd_timeout_seconds=argocd_sync_timeout_seconds
    )


def _get_deployed_host_urls( # pylint: disable=too-many-branches,too-many-nested-blocks
        manifest_path
):
    """Gets the ingress hosts URLs from a manifest of Kubernetes resources.

    Supports:

    - route.openshift.io/v1/Route
    - networking.k8s.io/v1/Ingress

    Returns
    -------
    list of str
        Ingress hosts URLs defined in the given manifest of Kubernetes resources.

    See
    ---
    * https://docs.openshift.com/container-platform/4.6/rest_api/network_apis/ingress-networking-k8s-io-v1.html
    * https://docs.openshift.com/container-platform/4.6/rest_api/network_apis/route-route-openshift-io-v1.html
    """ # pylint: disable=line-too-long
    host_urls = []
    manifest_resources = {}
    # load the manifest
    with open(manifest_path, encoding='utf-8') as file:
        manifest_resources = yaml.load_all(file, Loader=yaml.FullLoader)

        # for each resource in the manfest,
        # determine if its a known type and then attempt to get host and TLS config from it
        for manifest_resource in manifest_resources:
            if manifest_resource is None or 'kind' not in manifest_resource:
                continue

            kind = manifest_resource['kind']
            api_version = manifest_resource['apiVersion']

            # if Route resource
            if kind == 'Route' and api_version == 'route.openshift.io/v1':
                # get host
                if 'host' in manifest_resource['spec']:
                    host = manifest_resource['spec']['host']

                    # determine if TLS route
                    tls = False
                    if 'tls' in manifest_resource['spec']:
                        tls_config = manifest_resource['spec']['tls']
                        if tls_config:
                            tls = True

                    # determine protocol
                    protocol = ''
                    if tls:
                        protocol = 'https://'
                    else:
                        protocol = 'http://'

                    # record the host url
                    host_urls.append(f"{protocol}{host}")

            # if Ingress resource
            if kind == 'Ingress' and api_version == 'networking.k8s.io/v1':
                ingress_rules = manifest_resource['spec']['rules']
                for rule in ingress_rules:
                    # get host
                    if 'host' in rule:
                        host = rule['host']

                        # determine if TLS ingress
                        tls = False
                        if 'tls' in manifest_resource['spec']:
                            for tls_config in manifest_resource['spec']['tls']:
                                if ('hosts' in tls_config) and (host in tls_config['hosts']):
                                    tls = True
                                    break

                        # determine protocol
                        protocol = ''
                        if tls:
                            protocol = 'https://'
                        else:
                            protocol = 'http://'

                        # record the host url
                        host_urls.append(f"{protocol}{host}")

    return host_urls


def _argocd_get_app_manifest(
        self,
        argocd_app_name,
        source='live'
):
    """Get ArgoCD Application manifest.

    Parameters
    ----------
    argocd_app_name : str
        Name of the ArgoCD Application to get the manifest for.
    source : str (live,git)
        Get the manifest from the 'live' version of the 'git' version.

    Returns
    -------
    str
        Path to the retrieved ArgoCD manifest file.

    Raises
    ------
    StepRunnerException
        If error getting ArgoCD manifest.
    """
    argocd_app_manifest_file = self.write_working_file('deploy_argocd_manifests.yml')
    try:
        sh.argocd.app.manifests(  # pylint: disable=no-member
            f'--source={source}',
            argocd_app_name,
            _out=argocd_app_manifest_file,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise f"Error reading ArgoCD Application ({argocd_app_name}) manifest: {error}"

    return argocd_app_manifest_file


def deploy():  # pylint: disable=too-many-locals, too-many-statements

    results = {}

    # get input
    deployment_config_repo = 'http://gitea.tssc.rht-set.com/ploigos-reference-applications/reference-quarkus-mvn-cloud-resources_tekton_workflow-minimal.git'
    deployment_config_repo_branch = 'main'
    deployment_config_helm_chart_path = 'charts/reference-quarkus-mvn-deploy'
    deployment_config_destination_cluster_uri = 'https://kubernetes.default.svc'
    deployment_config_destination_cluster_token = '' # self.get_value('kube-api-token')
    deployment_config_helm_chart_environment_values_file = 'values-DEV.yaml'
    deployment_config_helm_chart_values_file_container_image_address_yq_path = 'image.tag'
    deployment_config_helm_chart_additional_value_files = ''
    additional_helm_values_files = ''
    argocd_app_name = 'tekton-task-app'
    container_image_address = ''

    git_email = ''
    git_name = ''
    git_username = ''
    git_password = ''

    environment = 'DEV'

    argocd_api=''
    argocd_username=''
    argocd_password=''
    argocd_skip_tls= ''
    deployment_namespace = 'argocd-deploy-task'
    argocd_auto_sync=True
    argocd_project = 'argocd-deployment-task-target'
    argocd_sync_timeout_seconds=60
    argocd_sync_retry_limit=20
    argocd_sync_prune=False
    work_dir_path = '.'

    results['argocd-app-name'] = 'argocd_app_name'
    results['container-image-deployed-address'] = container_image_address

    try:

        # clone the configuration repository
        print("Clone the configuration repository")
        repo_dir = create_working_dir_sub_dir('.', 'deployment-config-repo')
        deployment_config_repo_dir = clone_repo(
            repo_dir= repo_dir,
            repo_url=deployment_config_repo,
            repo_branch=deployment_config_repo_branch,
            git_email = git_email,
            git_name= git_name,
            username = git_username,
            password = git_password
        )

        # update values file, commit it, push it, and tag it
        print("Update the environment values file")
        deployment_config_helm_chart_environment_values_file_path = os.path.join(
            deployment_config_repo_dir,
            deployment_config_helm_chart_path,
            deployment_config_helm_chart_environment_values_file
        )
        _update_yaml_file_value(
            work_dir_path = work_dir_path,
            file=deployment_config_helm_chart_environment_values_file_path,
            yq_path=deployment_config_helm_chart_values_file_container_image_address_yq_path,
            value=container_image_address
        )

        print("Commit the updated environment values file")
        _git_commit_file(
            git_commit_message=f'Updating values for deployment to {environment}',
            file_path=os.path.join(
                deployment_config_helm_chart_path,
                deployment_config_helm_chart_environment_values_file
            ),
            repo_dir=deployment_config_repo_dir
        )
        print("Push the updated environment values file")
        deployment_config_repo_tag = 'DO NOT USE'
        _git_push_deployment_config_repo(
            deployment_config_repo=deployment_config_repo,
            deployment_config_repo_dir=deployment_config_repo_dir
        )
        # TODO: capture pushed commit hash in results


        # create/update argocd app and sync it
        print("Sign into ArgoCD")
        _argocd_sign_in(
            argocd_api=argocd_api,
            username=argocd_username,
            password=argocd_password,
            insecure=argocd_skip_tls
        )

        print(f'Deploying to namespace: {deployment_namespace}')

        print(f"Create or update ArgoCD Application ({argocd_app_name})")
        argocd_values_files = []
        argocd_values_files += deployment_config_helm_chart_additional_value_files
        argocd_values_files += [deployment_config_helm_chart_environment_values_file]
        argocd_values_files += additional_helm_values_files
        _argocd_app_create_or_update(
            argocd_app_name=argocd_app_name,
            repo=deployment_config_repo,
            revision=deployment_config_repo_tag,
            path=deployment_config_helm_chart_path,
            dest_server=deployment_config_destination_cluster_uri,
            dest_namespace=deployment_namespace,
            auto_sync=argocd_auto_sync,
            values_files=argocd_values_files,
            project=argocd_project
        )

        # sync and wait for the sync of the ArgoCD app
        print(f"Sync (and wait for) ArgoCD Application ({argocd_app_name})")
        _argocd_app_sync(
            argocd_app_name=argocd_app_name,
            argocd_sync_timeout_seconds=argocd_sync_timeout_seconds,
            argocd_sync_retry_limit=argocd_sync_retry_limit,
            argocd_sync_prune=argocd_sync_retry_limit
        )

        # get the ArgoCD app manifest that was synced
        print(f"Get ArgoCD Application ({argocd_app_name}) synced manifest")
        argocd_app_manifest_file = _argocd_get_app_manifest(
            argocd_app_name=argocd_app_name
        )
        results['argocd-deployed-manifest'] = argocd_app_manifest_file

        # determine the deployed host URLs
        print(
            "Determine the deployed host URLs for the synced"
            f" ArgoCD Application (({argocd_app_name})"
        )
        deployed_host_urls = _get_deployed_host_urls(
            manifest_path=argocd_app_manifest_file
        )
        results['deployed-host-urls'] = deployed_host_urls

    except RuntimeError as error:
        results['success'] = False
        results['message'] = f"Error deploying to environment ({environment}):" \
                              f" {str(error)}"

    return results


# Run the script
task_results = deploy()
print(task_results)
