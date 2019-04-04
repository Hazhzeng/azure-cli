# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import time
import re
import json
from knack.prompting import prompt_choice_list, prompt_y_n, prompt
from knack.util import CLIError
from azure_functions_devops_build.constants import (LINUX_CONSUMPTION, LINUX_DEDICATED, WINDOWS,
                                                    PYTHON, NODE, DOTNET)
from azure_functions_devops_build.exceptions import (
    GitOperationException,
    RoleAssignmentException,
    LanguageNotSupportException,
    ReleaseErrorException,
    GithubContentNotFound,
    GithubUnauthorizedError,
    GithubIntegrationRequestError
)
from .azure_devops_build_provider import AzureDevopsBuildProvider
from .custom import list_function_app, show_webapp, get_app_settings
from .utils import str2bool

# pylint: disable=too-many-instance-attributes

SUPPORTED_SCENARIOS = ['AZURE_DEVOPS', 'GITHUB_INTEGRATION']
SUPPORTED_SOURCECODE_LOCATIONS = ['Current Directory', 'Github']
SUPPORTED_LANGUAGES = {
    'python': PYTHON,
    'node': NODE,
    'dotnet': DOTNET,
}


class AzureDevopsBuildInteractive(object):
    """Implement the basic user flow for a new user wanting to do an Azure DevOps build for Azure Functions

    Attributes:
        cmd : the cmd input from the command line
        logger : a knack logger to log the info/error messages
    """

    def __init__(self, cmd, logger, functionapp_name, organization_name, project_name, repository_name,
                 overwrite_yaml, allow_force_push, use_local_settings, github_pat, github_repository):
        self.adbp = AzureDevopsBuildProvider(cmd.cli_ctx)
        self.cmd = cmd
        self.logger = logger
        self.cmd_selector = CmdSelectors(cmd, logger, self.adbp)
        self.functionapp_name = functionapp_name
        self.storage_name = None
        self.resource_group_name = None
        self.functionapp_language = None
        self.functionapp_type = None
        self.organization_name = organization_name
        self.project_name = project_name
        self.repository_name = repository_name

        self.github_pat = github_pat
        self.github_repository = github_repository
        self.github_service_endpoint_name = None

        self.repository_remote_name = None
        self.service_endpoint_name = None
        self.build_definition_name = None
        self.release_definition_name = None
        self.build_pool_name = "Default"
        self.release_pool_name = "Hosted VS2017"
        self.artifact_name = "drop"

        self.settings = []
        self.build = None
        self.release = None
        # These are used to tell if we made new objects
        self.scenario = None  # see SUPPORTED_SCENARIOS
        self.created_organization = False
        self.created_project = False
        self.overwrite_yaml = str2bool(overwrite_yaml)
        self.allow_force_push = allow_force_push
        self.use_local_settings = str2bool(use_local_settings)

    def interactive_azure_devops_build(self):
        """Main interactive flow which is the only function that should be used outside of this
        class (the rest are helpers)"""

        scenario = self.check_scenario()
        if scenario == 'AZURE_DEVOPS':
            return self.azure_devops_flow()
        elif scenario == 'GITHUB_INTEGRATION':
            return self.github_flow()
        raise CLIError('Unknown scenario')

    def azure_devops_flow(self):
        self.process_functionapp()
        self.pre_checks_azure_devops()
        self.process_organization()
        self.process_project()
        self.process_yaml_local()
        self.process_local_repository()
        self.process_remote_repository()
        self.process_functionapp_service_endpoint('AZURE_DEVOPS')
        self.process_extensions()
        self.process_build_and_release_definition_name('AZURE_DEVOPS')
        self.process_build('AZURE_DEVOPS')
        self.process_release()
        self.logger.warning("To trigger a function build again, please use")
        self.logger.warning("'git push {remote} master'".format(remote=self.repository_remote_name))
        return {
            'source_location': 'local',
            'functionapp_name': self.functionapp_name,
            'storage_name': self.storage_name,
            'resource_group_name': self.resource_group_name,
            'functionapp_language': self.functionapp_language,
            'functionapp_type': self.functionapp_type,
            'organization_name': self.organization_name,
            'project_name': self.project_name,
            'repository_name': self.repository_name,
            'service_endpoint_name': self.service_endpoint_name,
            'build_definition_name': self.build_definition_name,
            'release_definition_name': self.release_definition_name
        }

    def github_flow(self):
        self.process_github_personal_access_token()
        self.process_github_repository()
        self.process_functionapp()
        self.pre_checks_github()
        self.process_organization()
        self.process_project()
        self.process_yaml_github()
        self.process_functionapp_service_endpoint('GITHUB_INTEGRATION')
        self.process_github_service_endpoint()
        self.process_extensions()
        self.process_build_and_release_definition_name('GITHUB_INTEGRATION')
        self.process_build('GITHUB_INTEGRATION')
        self.process_release()
        self.logger.warning("Setup continuous integration between {github_repo} and Azure DevOps pipelines".format(
            github_repo=self.github_repository
        ))
        return {
            'source_location': 'Github',
            'functionapp_name': self.functionapp_name,
            'storage_name': self.storage_name,
            'resource_group_name': self.resource_group_name,
            'functionapp_language': self.functionapp_language,
            'functionapp_type': self.functionapp_type,
            'organization_name': self.organization_name,
            'project_name': self.project_name,
            'repository_name': self.github_repository,
            'service_endpoint_name': self.service_endpoint_name,
            'build_definition_name': self.build_definition_name,
            'release_definition_name': self.release_definition_name
        }

    def check_scenario(self):
        if self.repository_name:
            self.scenario = 'AZURE_DEVOPS'
        elif self.github_pat or self.github_repository:
            self.scenario = 'GITHUB_INTEGRATION'
        else:
            choice_index = prompt_choice_list(
                'Please choose Azure function source code location: ',
                SUPPORTED_SOURCECODE_LOCATIONS
            )
            self.scenario = SUPPORTED_SCENARIOS[choice_index]
        return self.scenario

    def pre_checks_azure_devops(self):
        if not self.adbp.check_git():
            raise CLIError("The program requires git source control to operate, please install git.")

        if not os.path.exists('host.json'):
            raise CLIError("There is no host.json in the current directory.{ls}"
                           "Functionapps must contain a host.json in their root".format(ls=os.linesep))

        if not os.path.exists('local.settings.json'):
            raise CLIError("There is no local.settings.json in the current directory.{ls}"
                           "Functionapps must contain a local.settings.json in their root".format(ls=os.linesep))

        local_runtime_language = self._find_local_repository_runtime_language()
        if local_runtime_language != self.functionapp_language:
            raise CLIError("The local language you are using ({setting}) does not match the language of your function app ({functionapps}){ls}"
                           "Please look at the FUNCTIONS_WORKER_RUNTIME both in your local.settings.json "
                           " and in your application settings on your functionapp in Azure.".format(
                               setting=local_runtime_language,
                               functionapps=self.functionapp_language,
                           ))

    def pre_checks_github(self):
        if not self.adbp.check_github_file(self.github_pat, self.github_repository, 'host.json'):
            raise CLIError("There is no host.json in Github repository {repo}.{ls}"
                           "Functionapps repository must contain a host.json in their root.{ls}"
                           "Please ensure you have read permission to the repository.".format(
                               repo=self.github_repository,
                               ls=os.linesep,
                           ))

        github_runtime_language = self._find_github_repository_runtime_language()
        if github_runtime_language is not None and github_runtime_language != self.functionapp_language:
            raise CLIError("The local language you are using ({setting}) does not match the language of your function app ({functionapps}){ls}"
                           "Please look at the FUNCTIONS_WORKER_RUNTIME both in your local.settings.json "
                           " and in your application settings on your functionapp in Azure.".format(
                               setting=github_runtime_language,
                               functionapps=self.functionapp_language,
                           ))

    def process_functionapp(self):
        """Helper to retrieve information about a functionapp"""
        if self.functionapp_name is None:
            functionapp = self._select_functionapp()
            self.functionapp_name = functionapp.name
        else:
            functionapp = self.cmd_selector.cmd_functionapp(self.functionapp_name)

        kinds = show_webapp(self.cmd, functionapp.resource_group, functionapp.name).kind.split(',')

        # Get functionapp settings in Azure
        app_settings = get_app_settings(self.cmd, functionapp.resource_group, functionapp.name)

        self.resource_group_name = functionapp.resource_group
        self.functionapp_type = self._find_type(kinds)

        try:
            self.functionapp_language = self._get_functionapp_runtime_language(app_settings)
            self.storage_name = self._get_functionapp_storage_name(app_settings)
        except LanguageNotSupportException as lnse:
            raise CLIError("Sorry, currently we do not support {language}.".format(language=lnse.message))

    def process_organization(self):
        """Helper to retrieve information about an organization / create a new one"""
        if self.organization_name is None:
            response = prompt_y_n('Would you like to use an existing Azure Devops organization? ')
            if response:
                self._select_organization()
            else:
                self._create_organization()
                self.created_organization = True
        else:
            self.cmd_selector.cmd_organization(self.organization_name)

    def process_project(self):
        """Helper to retrieve information about a project / create a new one"""
        # There is a new organization so a new project will be needed
        if (self.project_name is None) and (self.created_organization):
            self._create_project()
        elif self.project_name is None:
            use_existing_project = prompt_y_n('Would you like to use an existing Azure Devops project? ')
            if use_existing_project:
                self._select_project()
            else:
                self._create_project()
        else:
            self.cmd_selector.cmd_project(self.organization_name, self.project_name)

    def process_yaml_local(self):
        """Helper to create the local azure-pipelines.yml file"""
        # Try and get what the app settings are
        with open('local.settings.json') as f:
            data = json.load(f)

        default = ['FUNCTIONS_WORKER_RUNTIME', 'AzureWebJobsStorage']
        settings = []
        for key, value in data['Values'].items():
            if key not in default:
                settings.append((key, value))

        if settings:
            if self.use_local_settings is None:
                use_local_settings = prompt_y_n('Would you like to copy your local settings to your application in Azure?')  # pylint: disable=line-too-long
            else:
                use_local_settings = self.use_local_settings
            if not use_local_settings:
                settings = []

        self.settings = settings

        if os.path.exists('azure-pipelines.yml'):
            if self.overwrite_yaml is None:
                self.logger.warning("There is already an azure-pipelines.yml file in your local repository.")
                self.logger.warning("If you are using a yaml file that was not configured through this command this process may fail.")
                response = prompt_y_n("Do you want to delete it and create a new one? ")
            else:
                response = self.overwrite_yaml

        if (not os.path.exists('azure-pipelines.yml')) or response:
            self.logger.warning('Creating new azure-pipelines.yml')
            try:
                self.adbp.create_yaml(self.functionapp_language, self.functionapp_type)
            except LanguageNotSupportException as lnse:
                raise CLIError("Sorry, currently we do not support {language}.".format(language=lnse.message))

    def process_yaml_github(self):
        does_yaml_file_exist = self.adbp.check_github_file(self.github_pat, self.github_repository, "azure-pipelines.yml")
        if does_yaml_file_exist and self.overwrite_yaml is None:
            self.logger.warning("There is already an azure-pipelines.yml file in your Github repository.")
            self.logger.warning("If you are using a yaml file that was not configured through this command this process may fail.")
            self.overwrite_yaml = prompt_y_n("Do you want to generate a new one? (will commit to master branch) ")

        # Create and commit the new yaml file to Github without asking
        if not does_yaml_file_exist:
            if self.github_repository:
                self.logger.warning("Creating new azure-pipelines.yml for Github repository")
            try:
                return self.adbp.create_github_yaml(
                    pat=self.github_pat,
                    language=self.functionapp_language,
                    app_type=self.functionapp_type,
                    repository_fullname=self.github_repository
                )
            except LanguageNotSupportException as lnse:
                raise CLIError("Sorry, currently we do not support {language}.".format(language=lnse.message))
            except GithubUnauthorizedError:
                raise CLIError("Sorry, you do not have sufficient permission to commit azure-pipelines.yml to your Github repository.")

        # Overwrite yaml file
        if does_yaml_file_exist and self.overwrite_yaml:
            self.logger.warning("Overwrite azure-pipelines.yml file in Github repository")
            try:
                return self.adbp.create_github_yaml(
                    pat=self.github_pat,
                    language=self.functionapp_language,
                    app_type=self.functionapp_type,
                    repository_fullname=self.github_repository,
                    overwrite=True
                )
            except LanguageNotSupportException as lnse:
                raise CLIError("Sorry, currently we do not support {language}.".format(language=lnse.message))
            except GithubUnauthorizedError:
                raise CLIError("Sorry, you do not have sufficient permission to overwrite azure-pipelines.yml in your Github repository.")

    def process_local_repository(self):
        has_local_git_repository = self.adbp.check_git_local_repository()
        if has_local_git_repository:
            self.logger.warning("Detected local git repository.")

        # Collect repository name on Azure Devops
        if not self.repository_name:
            self.repository_name = prompt("Push to which Azure Devops repository (default: {repo}): ".format(repo=self.project_name))
            if not self.repository_name:  # Select default value
                self.repository_name = self.project_name

        expected_remote_name = self.adbp.get_local_git_remote_name(self.organization_name, self.project_name, self.repository_name)
        expected_remote_url = self.adbp.get_azure_devops_repo_url(self.organization_name, self.project_name, self.repository_name)

        # If local repository already has a remote
        # Let the user to know s/he can push to the remote directly for context update
        # Or let s/he remove the git remote manually
        has_local_git_remote = self.adbp.check_git_remote(self.organization_name, self.project_name, self.repository_name)
        if has_local_git_remote:
            raise CLIError("There's a git remote bound to {url}.{ls}"
                           "To update the repository and trigger an Azure Devops build, please use 'git push {remote} master'".format(
                               url=expected_remote_url,
                               remote=expected_remote_name,
                               ls=os.linesep)
                           )

        # Setup a local git repository and create a new commit on top of this context
        try:
            self.adbp.setup_local_git_repository(self.organization_name, self.project_name, self.repository_name)
        except GitOperationException:
            raise CLIError("Failed to setup local git repository.")

        self.repository_remote_name = expected_remote_name
        self.logger.warning("Added git remote {remote}".format(remote=expected_remote_name))

    def process_remote_repository(self):
        # Create remote repository if it does not exist
        repository = self.adbp.get_azure_devops_repository(self.organization_name, self.project_name, self.repository_name)
        if not repository:
            self.adbp.create_repository(self.organization_name, self.project_name, self.repository_name)

        # Force push branches if repository is not clean
        remote_url = self.adbp.get_azure_devops_repo_url(self.organization_name, self.project_name, self.repository_name)
        remote_branches = self.adbp.get_azure_devops_repository_branches(self.organization_name, self.project_name, self.repository_name)
        is_force_push = self._check_if_force_push_required(remote_url, remote_branches)

        # Prompt user to generate a git credential
        self._check_if_git_credential_required()

        # If the repository does not exist, we will do a normal push
        # If the repository exists, we will do a force push
        try:
            self.adbp.push_local_to_azure_devops_repository(self.organization_name, self.project_name, self.repository_name, force=is_force_push)
        except GitOperationException:
            self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
            raise CLIError("Failed to push your local repository to {url}{ls}"
                           "Please check your credentials and ensure you are a contributor to the repository.".format(url=remote_url, ls=os.linesep))

        self.logger.warning("Local branches has been pushed to {url}".format(url=remote_url))

    def process_github_personal_access_token(self):
        if not self.github_pat:
            self.logger.warning("If you need to create a Github Personal Access Token, please follow the following steps:")
            self.logger.warning("https://help.github.com/en/articles/creating-a-personal-access-token-for-the-command-line")
            self.logger.warning("The required Personal Access Token permission can be found here:")
            self.logger.warning("https://docs.microsoft.com/en-us/azure/devops/pipelines/repos/github?view=azure-devops#repository-permissions-for-personal-access-token-pat-authentication")

        while not self.github_pat or not self.adbp.check_github_pat(self.github_pat):
            self.github_pat = prompt(msg="Github Personal Access Token: ")
        print("Successfully validate Github personal access token.")

    def process_github_repository(self):
        while not self.github_repository or not self.adbp.check_github_repository(self.github_pat, self.github_repository):
            self.github_repository = prompt(msg="Github Repository (e.g. Azure/azure-cli): ")
        print("Successfully validate Github repository existence.")

    def process_build_and_release_definition_name(self, scenario):
        if scenario == 'AZURE_DEVOPS':
            self.build_definition_name = self.repository_remote_name.replace("_azuredevops_", "_build_", 1)[0:256]
            self.release_definition_name = self.repository_remote_name.replace("_azuredevops_", "_release_", 1)[0:256]
        if scenario == 'GITHUB_INTEGRATION':
            self.build_definition_name = "_build_github_" + self.github_repository.replace("/", "_", 1)[0:256]
            self.release_definition_name = "_release_github_" + self.github_repository.replace("/", "_", 1)[0:256]

    def process_github_service_endpoint(self):
        service_endpoints = self.adbp.get_github_service_endpoints(
            self.organization_name, self.project_name, self.github_repository
        )

        if not service_endpoints:
            service_endpoint = self.adbp.create_github_service_endpoint(
                self.organization_name, self.project_name, self.github_repository, self.github_pat
            )
        else:
            service_endpoint = service_endpoints[0]
            self.logger.warning("Detected Github service endpoint {name}".format(name=service_endpoint.name))

        self.github_service_endpoint_name = service_endpoint.name

    def process_functionapp_service_endpoint(self, scenario):
        repository = self.repository_name if scenario == "AZURE_DEVOPS" else self.github_repository
        service_endpoints = self.adbp.get_service_endpoints(
            self.organization_name, self.project_name, repository
        )

        # If there is no matching service endpoint, we need to create a new one
        if not service_endpoints:
            try:
                service_endpoint = self.adbp.create_service_endpoint(
                    self.organization_name, self.project_name, repository
                )
            except RoleAssignmentException:
                if scenario == "AZURE_DEVOPS":
                    self.adbp.remove_git_remote(self.organization_name, self.project_name, repository)
                raise CLIError("To use the Azure DevOps Pipeline Build,{ls}"
                               "We need to assign a contributor role to the Azure Functions release service principle.{ls}"
                               "Please ensure you are the owner of the subscription, or have role assignment write permission.".format(ls=os.linesep))
        else:
            service_endpoint = service_endpoints[0]
            self.logger.warning("Detected functionapp service endpoint {name}".format(name=service_endpoint.name))

        self.service_endpoint_name = service_endpoint.name

    def process_extensions(self):
        if self.functionapp_type == LINUX_CONSUMPTION:
            self.logger.warning("Installing the required extensions for the build and release")
            self.adbp.create_extension(self.organization_name, 'AzureAppServiceSetAppSettings', 'hboelman')
            self.adbp.create_extension(self.organization_name, 'PascalNaber-Xpirit-CreateSasToken', 'pascalnaber')

    def process_build(self, scenario):
        # need to check if the build definition already exists
        build_definitions = self.adbp.list_build_definitions(self.organization_name, self.project_name)
        build_definition_match = [
            build_definition for build_definition in build_definitions
            if build_definition.name == self.build_definition_name
        ]

        if not build_definition_match:
            if scenario == "AZURE_DEVOPS":
                self.adbp.create_devops_build_definition(self.organization_name, self.project_name,
                                                         self.repository_name, self.build_definition_name,
                                                         self.build_pool_name)
            elif scenario == "GITHUB_INTEGRATION":
                try:
                    self.adbp.create_github_build_definition(self.organization_name, self.project_name,
                                                             self.github_repository, self.build_definition_name,
                                                             self.build_pool_name)
                except GithubIntegrationRequestError as gire:
                    raise CLIError("{error}{ls}"
                                   "Please ensure your Github personal access token has sufficient permissions.{ls}"
                                   "You may visit https://docs.microsoft.com/en-us/azure/devops/pipelines/repos/"
                                   "github?view=azure-devops#repository-permissions-for-personal-access-token-pat-authentication"
                                   " for more information".format(
                                       error=gire.message, ls=os.linesep
                                   ))
        else:
            self.logger.warning("Detected build definition {name}".format(name=self.build_definition_name))

        self.build = self.adbp.create_build_object(
            self.organization_name,
            self.project_name,
            self.build_definition_name,
            self.build_pool_name
        )

        url = "https://dev.azure.com/{org}/{proj}/_build/results?buildId={build_id}".format(org=self.organization_name, proj=self.project_name, build_id=self.build.id)
        self.logger.warning("To follow the build process go to {url}".format(url=url))

    def process_release(self):
        # wait for artifacts / build to complete
        counter = 0
        build = None
        while build is None or build.result is None:
            time.sleep(5)
            build = self._get_build_by_id(self.organization_name, self.project_name, self.build.id)
            self.logger.warning("building artifacts ... {counter}s ({status})".format(counter=counter, status=build.status))
            counter += 5

        if build.result == 'failed':
            url = "https://dev.azure.com/{org}/{proj}/_build/results?buildId={build_id}".format(
                org=self.organization_name,
                proj=self.project_name,
                build_id=build.id
            )
            raise CLIError("Sorry, your build has failed in Azure Devops.{ls}"
                           "To view details on why your build has failed please visit {url}".format(url=url, ls=os.linesep))
        elif build.result == 'succeeded':
            self.logger.warning("Your build has completed. Composing a release definition...")

        # need to check if the release definition already exists
        release_definitions = self.adbp.list_release_definitions(self.organization_name, self.project_name)
        release_definition_match = [
            release_definition for release_definition in release_definitions
            if release_definition.name == self.release_definition_name
        ]

        if not release_definition_match:
            self.adbp.create_release_definition(self.organization_name, self.project_name,
                                                self.build_definition_name, self.artifact_name,
                                                self.release_pool_name, self.service_endpoint_name,
                                                self.release_definition_name, self.functionapp_type,
                                                self.functionapp_name, self.storage_name,
                                                self.resource_group_name, self.settings)
        else:
            self.logger.warning("Detected release definition {name}".format(name=self.release_definition_name))

        # The build artifact takes some time to propagate
        self.logger.warning("Prepare to release the artifact...")
        time.sleep(5)

        try:
            release = self.adbp.create_release(self.organization_name, self.project_name, self.release_definition_name)
        except ReleaseErrorException:
            url = "https://dev.azure.com/{org}/{proj}/_release".format(org=self.organization_name, proj=self.project_name)
            raise CLIError("Sorry, your release has failed in Azure Devops.{ls}"
                           "To view details on why your release has failed please visit {url}".format(url=url, ls=os.linesep))

        url = "https://dev.azure.com/{org}/{proj}/_releaseProgress?_a=release-environment-logs&releaseId={release_id}".format(
            org=self.organization_name,
            proj=self.project_name,
            release_id=release.id
        )
        self.logger.warning("To follow the release process go to {url}".format(url=url))
        self.release = release

    def _check_if_force_push_required(self, remote_url, remote_branches):
        force_push_required = False
        if remote_branches:
            self.logger.warning("The remote repository is not clean: {url}".format(url=remote_url))
            self.logger.warning("If you wish to continue, a force push will be commited and your local branches will overwrite the remote branches!")
            self.logger.warning("Please ensure you have force push permission in {repo} repository.".format(repo=self.repository_name))

            if self.allow_force_push is None:
                consent = prompt_y_n("I consent to force push all local branches to Azure Devops repository")
            else:
                consent = str2bool(self.allow_force_push)

            if not consent:
                self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
                raise CLIError("Failed to obtain your consent.")
            else:
                force_push_required = True

        return force_push_required

    def _check_if_git_credential_required(self):
        # Username and password are not required if git credential manager exists
        if self.adbp.check_git_credential_manager():
            return

        # Manual setup alternative credential in Azure Devops
        self.logger.warning("Please visit https://dev.azure.com/{org}/_usersSettings/altcreds".format(
            org=self.organization_name,
        ))
        self.logger.warning('Check "Enable alternate authentication credentials" and save your username and password.')
        self.logger.warning("You may need to use this credential when pushing your code to Azure Devops repository.")
        consent = prompt_y_n("I have setup alternative authentication credentials for {repo}".format(repo=self.repository_name))
        if not consent:
            self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
            raise CLIError("Failed to obtain your consent.")

    def _select_functionapp(self):
        self.logger.info("Retrieving functionapp names.")
        functionapps = list_function_app(self.cmd)
        functionapp_names = sorted([functionapp.name for functionapp in functionapps])
        if len(functionapp_names) < 1:
            raise CLIError("You do not have any existing function apps associated with this account subscription.{ls}"
                           "1. Please make sure you are logged into the right azure account by running 'az account show' and checking the user.{ls}"
                           "2. If you are logged in as the right account please check the subscription you are using. Run 'az account show' and view the name.{ls}"
                           "   If you need to set the subscription run 'az account set --subscription {SUBSCRIPTION_NAME}'{ls}"
                           "3. If you do not have a function app please create one".format(ls=os.linesep))
        choice_index = prompt_choice_list('Please select the target function app: ', functionapp_names)
        functionapp = [functionapp for functionapp in functionapps
                       if functionapp.name == functionapp_names[choice_index]][0]
        self.logger.info("Selected functionapp %s", functionapp.name)
        return functionapp

    def _find_local_repository_runtime_language(self):
        # We want to check that locally the language that they are using matches the type of application they
        # are deploying to
        with open('local.settings.json') as f:
            settings = json.load(f)

        runtime_language = settings.get('Values', {}).get('FUNCTIONS_WORKER_RUNTIME')
        if not runtime_language:
            raise CLIError("The 'FUNCTIONS_WORKER_RUNTIME' setting is not defined in the local.settings.json file")

        if SUPPORTED_LANGUAGES.get(runtime_language):
            return runtime_language
        else:
            raise LanguageNotSupportException(runtime_language)

    def _find_github_repository_runtime_language(self):
        try:
            github_file_content = self.adbp.get_github_content(self.github_pat, self.github_repository, "local.settings.json")
        except GithubContentNotFound:
            self.logger.warning("The local.settings.json is not commited to Github repository {repo}".format(repo=self.github_repository))
            self.logger.warning("Functionapp worker runtime language check is disabled.")
            return None

        runtime_language = github_file_content.get('Values', {}).get('FUNCTIONS_WORKER_RUNTIME')
        if not runtime_language:
            raise CLIError("The 'FUNCTIONS_WORKER_RUNTIME' setting is not defined in the local.settings.json file")

        if SUPPORTED_LANGUAGES.get(runtime_language):
            return runtime_language
        else:
            raise LanguageNotSupportException(runtime_language)

    def _get_functionapp_runtime_language(self, app_settings):
        functions_worker_runtime = [
            setting['value'] for setting in app_settings if setting['name'] == "FUNCTIONS_WORKER_RUNTIME"
        ]

        if functions_worker_runtime:
            functionapp_language = functions_worker_runtime[0]
            if SUPPORTED_LANGUAGES.get(functionapp_language):
                return SUPPORTED_LANGUAGES[functionapp_language]
            else:
                raise LanguageNotSupportException(functionapp_language)
        return None

    def _get_functionapp_storage_name(self, app_settings):
        functions_worker_runtime = [
            setting['value'] for setting in app_settings if setting['name'] == "AzureWebJobsStorage"
        ]

        if functions_worker_runtime:
            return functions_worker_runtime[0].split(';')[1].split('=')[1]
        return None

    def _find_type(self, kinds):  # pylint: disable=no-self-use
        if 'linux' in kinds:
            if 'container' in kinds:
                functionapp_type = LINUX_DEDICATED
            else:
                functionapp_type = LINUX_CONSUMPTION
        else:
            functionapp_type = WINDOWS
        return functionapp_type

    def _select_organization(self):
        organizations = self.adbp.list_organizations()
        organization_names = sorted([organization.accountName for organization in organizations.value])
        if len(organization_names) < 1:
            self.logger.warning("There are no existing organizations, you need to create a new organization.")
            self._create_organization()
            self.created_organization = True
        else:
            choice_index = prompt_choice_list('Please choose the organization: ', organization_names)
            organization_match = [organization for organization in organizations.value
                                  if organization.accountName == organization_names[choice_index]][0]
            self.organization_name = organization_match.accountName

    def _get_organization_by_name(self, organization_name):
        organizations = self.adbp.list_organizations()
        return [organization for organization in organizations.value
                if organization.accountName == organization_name][0]

    def _create_organization(self):
        self.logger.info("Starting process to create a new Azure DevOps organization")
        regions = self.adbp.list_regions()
        region_names = sorted([region.display_name for region in regions.value])
        self.logger.info("The region for an Azure DevOps organization is where the organization will be located. Try locate it near your other resources and your location")  # pylint: disable=line-too-long
        choice_index = prompt_choice_list('Please select a region for the new organization: ', region_names)
        region = [region for region in regions.value if region.display_name == region_names[choice_index]][0]

        while True:
            organization_name = prompt("Please enter the name of the new organization: ")
            new_organization = self.adbp.create_organization(organization_name, region.name)
            if new_organization.valid is False:
                self.logger.warning(new_organization.message)
                self.logger.warning("Note: any name must be globally unique")
            else:
                break
        url = "https://dev.azure.com/" + new_organization.name + "/"
        self.logger.info("Finished creating the new organization. Click the link to see your new organization: %s", url)
        self.organization_name = new_organization.name

    def _select_project(self):
        projects = self.adbp.list_projects(self.organization_name)
        if projects.count > 0:
            project_names = sorted([project.name for project in projects.value])
            choice_index = prompt_choice_list('Please select your project: ', project_names)
            project = [project for project in projects.value if project.name == project_names[choice_index]][0]
            self.project_name = project.name
        else:
            self.logger.warning("There are no existing projects in this organization. You need to create a new project.")  # pylint: disable=line-too-long
            self._create_project()

    def _create_project(self):
        project_name = prompt("Please enter the name of the new project: ")
        project = self.adbp.create_project(self.organization_name, project_name)
        # Keep retrying to create a new project if it fails
        while not project.valid:
            self.logger.error(project.message)
            project_name = prompt("Please enter the name of the new project: ")
            project = self.adbp.create_project(self.organization_name, project_name)

        url = "https://dev.azure.com/" + self.organization_name + "/" + project.name + "/"
        self.logger.info("Finished creating the new project. Click the link to see your new project: %s", url)
        self.project_name = project.name
        self.created_project = True

    def _get_build_by_id(self, organization_name, project_name, build_id):
        builds = self.adbp.list_build_objects(organization_name, project_name)
        return next((build for build in builds if build.id == build_id))


class CmdSelectors(object):

    def __init__(self, cmd, logger, adbp):
        self.cmd = cmd
        self.logger = logger
        self.adbp = adbp

    def cmd_functionapp(self, functionapp_name):
        functionapps = list_function_app(self.cmd)
        functionapp_match = [functionapp for functionapp in functionapps
                             if functionapp.name == functionapp_name]
        if not functionapp_match:
            raise CLIError("Error finding functionapp. Please check that the functionapp exists using 'az functionapp list")
        else:
            functionapp = functionapp_match[0]
        return functionapp

    def cmd_organization(self, organization_name):
        organizations = self.adbp.list_organizations()
        organization_match = [organization for organization in organizations.value
                              if organization.accountName == organization_name]
        if not organization_match:
            raise CLIError("Error finding organization. Please check that the organization exists by logging onto your dev.azure.com acocunt")
        else:
            organization = organization_match[0]
        return organization

    def cmd_project(self, organization_name, project_name):
        projects = self.adbp.list_projects(organization_name)
        project_match = \
            [project for project in projects.value if project.name == project_name]

        if not project_match:
            raise CLIError("Error finding project. Please check that the project exists by logging onto your dev.azure.com acocunt")
        else:
            project = project_match[0]
        return project
