# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
import json
import unittest
import jmespath
import mock
import uuid
import os
import time
import tempfile
import requests

from azure_devtools.scenario_tests import AllowLargeResponse, record_only
from azure.cli.testsdk import (ScenarioTest, LiveScenarioTest, ResourceGroupPreparer,
                               StorageAccountPreparer, JMESPathCheck)

TEST_DIR = os.path.abspath(os.path.join(os.path.abspath(__file__), '..'))

# pylint: disable=line-too-long

class DevopsBuildCommandsTest(ScenarioTest):
    def setUp(self):
        super().setUp()

        # You must be the organization owner
        self.azure_devops_organization = "azure-functions-devops-build-test" 
        self.os_type = "Windows"
        self.runtime = "dotnet"

        self.functionapp = self.create_random_name(prefix='functionapp-e2e', length=24)
        self.azure_devops_project = self.create_random_name(prefix='test-project-e2e', length=24)
        self.azure_devops_repository = self.create_random_name(prefix='test-repository-e2e', length=24)

    @ResourceGroupPreparer()
    @StorageAccountPreparer(parameter_name='storage_account_for_test')
    def test_devops_build_command(self, resource_group, resource_group_location, storage_account_for_test):
        # Create a new functionapp
        self.cmd('functionapp create --resource-group {rg} --storage-account {sa}'
                ' --os-type {ot} --runtime {rt} --name {fn} --consumption-plan-location {cpl}'.format(
            rg=resource_group,
            sa=storage_account_for_test,
            ot=self.os_type,
            rt=self.runtime,
            fn=self.functionapp,
            cpl=resource_group_location
        ), checks=[
            JMESPathCheck('name', self.functionapp),
            JMESPathCheck('resourceGroup', resource_group),
        ])

        # Install azure devops extension
        self.cmd('extension add --name azure-devops');

        # Create a new project in Azure Devops
        result = self.cmd('devops project create --organization https://dev.azure.com/{org} --name {proj}'.format(
            org=self.azure_devops_organization,
            proj=self.azure_devops_project
        ), checks=[
            JMESPathCheck('name', self.azure_devops_project),
        ]).get_output_in_json()
        azure_devops_project_id = result['id']

        # Create a new repository in Azure Devops
        self.cmd('repos create --organization https://dev.azure.com/{org} --project {proj} --name {repo}'.format(
            org=self.azure_devops_organization,
            proj=self.azure_devops_project,
            repo=self.azure_devops_repository,
        ), checks=[
            JMESPathCheck('name', self.azure_devops_repository),
        ]).get_output_in_json()
        azure_devops_repository_id = result['id']

        # Remove Azure Devops project
        self.cmd('devops project delete --organization https://dev.azure.com/{org} --id {id} --yes'.format(
            org=self.azure_devops_organization,
            id=azure_devops_project_id
        ))