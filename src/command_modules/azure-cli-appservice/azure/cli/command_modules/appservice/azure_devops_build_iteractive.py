# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import time
import re
import json
from knack.prompting import prompt_choice_list, prompt_y_n, prompt
from azure_functions_devops_build.constants import (LINUX_CONSUMPTION, LINUX_DEDICATED, WINDOWS,
                                                    PYTHON, NODE, DOTNET, JAVA)
from azure_functions_devops_build.exceptions import (
    GitOperationException,
    RoleAssignmentException,
    LanguageNotSupportException
)
from .azure_devops_build_provider import AzureDevopsBuildProvider
from .custom import list_function_app, show_webapp, get_app_settings

# pylint: disable=too-many-instance-attributes


def str2bool(v):
    if v == 'true':
        retval = True
    elif v == 'false':
        retval = False
    else:
        retval = None
    return retval


class AzureDevopsBuildInteractive(object):
    """Implement the basic user flow for a new user wanting to do an Azure DevOps build for Azure Functions

    Attributes:
        cmd : the cmd input from the command line
        logger : a knack logger to log the info/error messages
    """

    def __init__(self, cmd, logger, functionapp_name, organization_name, project_name,
                 overwrite_yaml, use_local_settings, local_git):
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
        self.repository_name = None
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
        self.created_organization = False
        self.created_project = False
        self.overwrite_yaml = str2bool(overwrite_yaml)
        self.use_local_settings = str2bool(use_local_settings)
        self.local_git = local_git

    def interactive_azure_devops_build(self):
        """Main interactive flow which is the only function that should be used outside of this
        class (the rest are helpers)
        """
        self.pre_checks()
        self.process_functionapp()
        self.process_organization()
        self.process_project()

        # Generate Azure pipenline build yaml
        self.process_yaml()

        # Allow user to choose the uploading destination
        self.process_local_repository()
        self.process_remote_repository()

        # Set up the default names for the rest of the things we need to create
        self.process_service_endpoint()
        self.process_extensions()

        # Start build process and release artifacts to azure functions app
        self.process_build_and_release_definition_name()
        self.process_build()
        self.process_release()

        # Advise user to reuse the pipeline build and release by pushing to remote
        print()
        print("To trigger a function build again, please use")
        print("'git push {remote} master'".format(remote=self.repository_remote_name))
        print()

        return_dict = {}
        return_dict['functionapp_name'] = self.functionapp_name
        return_dict['storage_name'] = self.storage_name
        return_dict['resource_group_name'] = self.resource_group_name
        return_dict['functionapp_language'] = self.functionapp_language
        return_dict['functionapp_type'] = self.functionapp_type
        return_dict['organization_name'] = self.organization_name
        return_dict['project_name'] = self.project_name
        return_dict['repository_name'] = self.repository_name
        return_dict['service_endpoint_name'] = self.service_endpoint_name
        return_dict['build_definition_name'] = self.build_definition_name
        return_dict['release_definition_name'] = self.release_definition_name

        return return_dict

    def pre_checks(self):
        if not os.path.exists('host.json'):
            self.logger.critical("There is no host.json in the current directory.")
            self.logger.critical("Functionapps must contain a host.json in their root.")
            exit(1)

        if not self.adbp.check_git():
            self.logger.critical("The program requires git source control to operate, please install git.")
            exit(1)

    def process_functionapp(self):
        """Helper to retrieve information about a functionapp"""
        if self.functionapp_name is None:
            functionapp = self._select_functionapp()
            # We now know the functionapp name so can set it
            self.functionapp_name = functionapp.name
        else:
            functionapp = self.cmd_selector.cmd_functionapp(self.functionapp_name)

        kinds = show_webapp(self.cmd, functionapp.resource_group, functionapp.name).kind.split(',')
        app_settings = get_app_settings(self.cmd, functionapp.resource_group, functionapp.name)

        self.resource_group_name = functionapp.resource_group
        self.functionapp_type = self._find_type(kinds)

        try:
            self.functionapp_language, self.storage_name = self._find_language_and_storage_name(app_settings)
        except LanguageNotSupportException as lnse:
            self.logger.critical("Sorry, currently we do not support {language}.".format(language=lnse.message))
            exit(1)

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

    def process_yaml(self):
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
                self.logger.warning("There is already an azure pipelines yaml file.")
                self.logger.warning("If you are using a yaml file that was not configured through this command this process may fail.")  # pylint: disable=line-too-long
                response = prompt_y_n("Do you want to delete it and create a new one? ")
            else:
                response = self.overwrite_yaml
        if (not os.path.exists('azure-pipelines.yml')) or response:
            print('Creating new azure-pipelines.yml')
            try:
                self.adbp.create_yaml(self.functionapp_language, self.functionapp_type)
            except LanguageNotSupportException as lnse:
                self.logger.critical("Sorry, currently we do not support {language}.".format(language=lnse.message))
                exit(1)

    def process_local_repository(self):
        has_local_git_repository = self.adbp.check_git_local_repository()
        if has_local_git_repository:
            self.logger.warning("Detected local git repository.")

        # Collect repository name on Azure Devops
        expected_repository = prompt("Push to which Azure Devops repository (default: {repo}): ".format(
                repo=self.project_name))
        if not expected_repository:
            expected_repository = self.project_name

        expected_remote_name = self.adbp.get_local_git_remote_name(self.organization_name, self.project_name, expected_repository)
        expected_remote_url = self.adbp.get_azure_devops_repo_url(self.organization_name, self.project_name, expected_repository)

        # If local repository already has a remote
        # Let the user to know s/he can push to the remote directly for context update
        # Or let s/he remove the git remote manually
        has_local_git_remote = self.adbp.check_git_remote(self.organization_name, self.project_name, expected_repository)
        if has_local_git_remote:
            self.logger.warning("There's a git remote bound to {url}.".format(url=expected_remote_url))
            self.logger.warning("To update the repository and trigger an Azure Devops build, please use 'git push {remote} master'".format(remote=expected_remote_name))
            exit(1)

        # Setup a local git repository and create a new commit on top of this context
        self.repository_name = expected_repository
        try:
            self.adbp.setup_local_git_repository(self.organization_name, self.project_name, self.repository_name)
        except GitOperationException as goe:
            self.logger.fatal("Failed to setup local git repository.")
            self.logger.fatal(goe.message)
            exit(1)

        self.repository_remote_name = expected_remote_name
        print("Added git remote {remote}".format(remote=expected_remote_name))

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
        except GitOperationException as goe:
            self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
            self.logger.fatal("Failed to push your local repository to {url}".format(url=remote_url))
            self.logger.fatal("Please check your credentials and ensure you have sufficient permissions.")
            exit(0)

        print("Local branches has been pushed to {url}".format(url=remote_url))

    def process_build_and_release_definition_name(self):
        self.build_definition_name = self.repository_remote_name.replace("_azuredevops_", "_build_", 1)[0:256]
        self.release_definition_name = self.repository_remote_name.replace("_azuredevops_", "_release_", 1)[0:256]

    def process_service_endpoint(self):
        service_endpoints = self.adbp.get_service_endpoints(
            self.organization_name, self.project_name, self.repository_name
        )

        # If there is no matching service endpoint, we need to create a new one
        if not service_endpoints:
            try:
                service_endpoint = self.adbp.create_service_endpoint(
                    self.organization_name, self.project_name, self.repository_name
                )
            except RoleAssignmentException:
                self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
                self.logger.error("To use the Azure DevOps Pipeline Build,")
                self.logger.error("We need to assign a contributor role to the Azure Functions release service principle.")
                self.logger.error("Please ensure you are the owner of the subscription, or have role assignment write permission.")
                exit(1)
        else:
            service_endpoint = service_endpoints[0]
            self.logger.warning("Detected service endpoint {name}".format(name=service_endpoint.name))

        self.service_endpoint_name = service_endpoint.name

    def process_extensions(self):
        if self.functionapp_type == LINUX_CONSUMPTION:
            print("Installing the required extensions for the build and release")
            self.adbp.create_extension(self.organization_name, 'AzureAppServiceSetAppSettings', 'hboelman')
            self.adbp.create_extension(self.organization_name, 'PascalNaber-Xpirit-CreateSasToken', 'pascalnaber')

    def process_build(self):
        # need to check if the build definition already exists
        build_definitions = self.adbp.list_build_definitions(self.organization_name, self.project_name)
        build_definition_match = [
            build_definition for build_definition in build_definitions
            if build_definition.name == self.build_definition_name
        ]

        if not build_definition_match:
            self.adbp.create_build_definition(self.organization_name, self.project_name,
                                              self.repository_name, self.build_definition_name,
                                              self.build_pool_name)
        else:
            self.logger.warning("Detected build definition {name}".format(name=self.build_definition_name))

        self.build = self.adbp.create_build_object(self.organization_name, self.project_name,
                                              self.build_definition_name, self.build_pool_name)

        url = "https://dev.azure.com/{org}/{proj}/_build/results?buildId={build_id}".format(org=self.organization_name, proj=self.project_name, build_id=self.build.id)
        print("To follow the build process go to {url}".format(url=url))

    def process_release(self):
        # wait for artifacts / build to complete
        artifacts = []
        counter = 0
        while artifacts == []:
            time.sleep(5)
            print("building artifacts ... {counter}s".format(counter=counter))
            build = self._get_build_by_id(self.organization_name, self.project_name, self.build.id)
            if build.status == 'completed':
                break
            artifacts = self.adbp.list_artifacts(self.organization_name, self.project_name, self.build.id)
            counter += 5

        if build.result == 'failed':
            url = "https://dev.azure.com/{org}/{proj}/_build/results?buildId={build_id}".format(
                org=self.organization_name,
                proj=self.project_name,
                build_id=build.id
            )
            self.logger.critical("Sorry, your build has failed in Azure Devops.")
            self.logger.critical("To view details on why your build has failed please visit {url}".format(url=url))
            exit(1)

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
        time.sleep(5)

        try:
            release = self.adbp.create_release(self.organization_name, self.project_name, self.release_definition_name)
            url = "https://dev.azure.com/{org}/{proj}/_releaseProgress?_a=release-environment-logs&releaseId={release_id}".format(
                org=self.organization_name,
                proj=self.project_name,
                release_id=release.id
            )
        except Exception:
            self.logger.critical("Sorry, your release has failed in Azure Devops.")
            self.logger.critical("To view details on why your release has failed please visit {url}".format(url=url))
            exit(1)

        print("To follow the release process go to {url}".format(url=url))
        self.release = release

    def _check_if_force_push_required(self, remote_url, remote_branches):
        force_push_required = False
        if remote_branches:
            self.logger.warning("The remote repository is not clean: {url}".format(url=remote_url))
            self.logger.warning("If you wish to continue, a force push will be commited and your local branches will overwrite the remote branches!")
            self.logger.warning("Please ensure you have force push permission in {repo} repository.".format(repo=self.repository_name))
            consent = prompt_y_n("I consent to force push all local branches to Azure Devops repository")

            if not consent:
                self.adbp.remove_git_remote(self.organization_name, self.project_name, self.repository_name)
                exit(0)
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
            exit(0)

    def _select_functionapp(self):
        self.logger.info("Retrieving functionapp names.")
        functionapps = list_function_app(self.cmd)
        functionapp_names = sorted([functionapp.name for functionapp in functionapps])
        if len(functionapp_names) < 1:
            self.logger.critical("You do not have any existing function apps associated with this account subscription.")
            self.logger.critical("1. Please make sure you are logged into the right azure account by running `az account show` and checking the user.")  # pylint: disable=line-too-long
            self.logger.critical("2. If you are logged in as the right account please check the subscription you are using. Run `az account show` and view the name.")  # pylint: disable=line-too-long
            self.logger.critical("   If you need to set the subscription run `az account set --subscription \"{SUBSCRIPTION_NAME}\"`")  # pylint: disable=line-too-long
            self.logger.critical("3. If you do not have a function app please create one")
            exit(1)
        choice_index = prompt_choice_list('Please choose the functionapp: ', functionapp_names)
        functionapp = [functionapp for functionapp in functionapps
                       if functionapp.name == functionapp_names[choice_index]][0]
        self.logger.info("Selected functionapp %s", functionapp.name)
        return functionapp

    def _find_local_language(self):
        # We want to check that locally the language that they are using matches the type of application they
        # are deploying to
        with open('local.settings.json') as f:
            settings = json.load(f)
        try:
            local_language = settings['Values']['FUNCTIONS_WORKER_RUNTIME']
        except KeyError:
            self.logger.critical('The app \'FUNCTIONS_WORKER_RUNTIME\' setting is not set in the local.settings.json file')  # pylint: disable=line-too-long
            exit(1)
        if local_language == '':
            self.logger.critical('The app \'FUNCTIONS_WORKER_RUNTIME\' setting is not set in the local.settings.json file')  # pylint: disable=line-too-long
            exit(1)
        return local_language

    def _find_language_and_storage_name(self, app_settings):
        local_language = self._find_local_language()
        for app_setting in app_settings:
            if app_setting['name'] == "FUNCTIONS_WORKER_RUNTIME":
                language_str = app_setting['value']
                if language_str != local_language:
                    # We should not deploy if the local runtime language is not the same as that of their functionapp
                    self.logger.critical("The local language you are using ({local}) does not match the language of your function app ({functionapps})".format(local=local_language, functionapps=language_str))  # pylint: disable=line-too-long
                    self.logger.critical("Please look at the FUNCTIONS_WORKER_RUNTIME both in your local.settings.json and in your application settings on your function app in Azure.")  # pylint: disable=line-too-long
                    exit(1)
                if language_str == "python":
                    self.logger.info("detected that language used by functionapp is python")
                    language = PYTHON
                elif language_str == "node":
                    self.logger.info("detected that language used by functionapp is node")
                    language = NODE
                elif language_str == "dotnet":
                    self.logger.info("detected that language used by functionapp is .net")
                    language = DOTNET
                #elif language_str == "java":
                #    self.logger.info("detected that language used by functionapp is java")
                #    language = JAVA
                else:
                    raise LanguageNotSupportException(language_str)
            if app_setting['name'] == "AzureWebJobsStorage":
                storage_name = app_setting['value'].split(';')[1].split('=')[1]
                self.logger.info("detected that storage used by the functionapp is %s", storage_name)
        return language, storage_name

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
            self.logger.error("""Error finding functionapp. Please check that the functionapp exists using 'az functionapp list'""")  # pylint: disable=line-too-long
            exit(1)
        else:
            functionapp = functionapp_match[0]
        return functionapp

    def cmd_organization(self, organization_name):
        organizations = self.adbp.list_organizations()
        organization_match = [organization for organization in organizations.value
                              if organization.accountName == organization_name]
        if not organization_match:
            self.logger.error("""Error finding organization. Please check that the organization exists by logging onto your dev.azure.com acocunt""")  # pylint: disable=line-too-long
            exit(1)
        else:
            organization = organization_match[0]
        return organization

    def cmd_project(self, organization_name, project_name):
        projects = self.adbp.list_projects(organization_name)
        project_match = \
            [project for project in projects.value if project.name == project_name]

        if not project_match:
            self.logger.error("Error finding project. Please check that the project exists by logging onto your dev.azure.com acocunt")  # pylint: disable=line-too-long
            exit(1)
        else:
            project = project_match[0]
        return project
