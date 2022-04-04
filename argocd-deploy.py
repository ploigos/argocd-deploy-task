import os
import sh
import sys
import re

GIT_REPO_REGEX = re.compile(r"(?P<protocol>^https:\/\/|^http:\/\/)?(?P<address>.*$)")

def create_working_dir_sub_dir(self, sub_dir_relative_path=""):
    """Create a folder under the working/stepname folder.

    Returns
    -------
    str
        Path to created working sub directory.
    """
    file_path = os.path.join(self.work_dir_path, sub_dir_relative_path)
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


def _update_yaml_file_value(self, file, yq_path, value):
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
    yq_script_file = self.write_working_file(
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
    force_push_tags = 'true'
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

    results['argocd-app-name'] = 'argocd_app_name'
    results['container-image-deployed-address'] = container_image_address

    try:

        # clone the configuration repository
        print("Clone the configuration repository")
        repo_dir = create_working_dir_sub_dir('deployment-config-repo')
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
            file=deployment_config_helm_chart_environment_values_file_path,
            yq_path=deployment_config_helm_chart_values_file_container_image_address_yq_path,
            value=container_image_address
        )

        print("Commit the updated environment values file")
        _git_commit_file(
            git_commit_message=f'Updating values for deployment to {self.environment}',
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

        add_or_update_target_cluster = self.get_value('argocd-add-or-update-target-cluster')
        if add_or_update_target_cluster:
            print("Add target cluster to ArgoCD")
            self._argocd_add_target_cluster(
                kube_api=deployment_config_destination_cluster_uri,
                kube_api_token=deployment_config_destination_cluster_token,
                kube_api_skip_tls=self.get_value('kube-api-skip-tls')
            )

        print("Determine deployment namespace")
        deployment_namespace = self.get_value('deployment-namespace')
        if deployment_namespace:
            print(f"  Using user provided namespace name: {deployment_namespace}")
        else:
            deployment_namespace = argocd_app_name
            print(f"  Using auto generated namespace name: {deployment_namespace}")

        print(f"Create or update ArgoCD Application ({argocd_app_name})")
        argocd_values_files = []
        argocd_values_files += deployment_config_helm_chart_additional_value_files
        argocd_values_files += [deployment_config_helm_chart_environment_values_file]
        argocd_values_files += additional_helm_values_files
        self._argocd_app_create_or_update(
            argocd_app_name=argocd_app_name,
            repo=deployment_config_repo,
            revision=deployment_config_repo_tag,
            path=deployment_config_helm_chart_path,
            dest_server=deployment_config_destination_cluster_uri,
            dest_namespace=deployment_namespace,
            auto_sync=self.get_value('argocd-auto-sync'),
            values_files=argocd_values_files,
            project=self.get_value('argocd-project')
        )

        # sync and wait for the sync of the ArgoCD app
        print(f"Sync (and wait for) ArgoCD Application ({argocd_app_name})")
        self._argocd_app_sync(
            argocd_app_name=argocd_app_name,
            argocd_sync_timeout_seconds=self.get_value('argocd-sync-timeout-seconds'),
            argocd_sync_retry_limit=self.get_value('argocd-sync-retry-limit'),
            argocd_sync_prune=self.get_value('argocd-sync-prune')
        )

        # get the ArgoCD app manifest that was synced
        print(f"Get ArgoCD Application ({argocd_app_name}) synced manifest")
        argocd_app_manifest_file = self._argocd_get_app_manifest(
            argocd_app_name=argocd_app_name
        )
        step_result.add_artifact(
            name='argocd-deployed-manifest',
            value=argocd_app_manifest_file
        )

        # determine the deployed host URLs
        print(
            "Determine the deployed host URLs for the synced"
            f" ArgoCD Application (({argocd_app_name})"
        )
        deployed_host_urls = self._get_deployed_host_urls(
            manifest_path=argocd_app_manifest_file
        )
        step_result.add_artifact(
            name='deployed-host-urls',
            value=deployed_host_urls
        )
    except RuntimeError as error:
        results['success'] = False
        results['message'] = f"Error deploying to environment ({environment}):" \
                              f" {str(error)}"

    return results


deploy()
