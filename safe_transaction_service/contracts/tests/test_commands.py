from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from safe_eth.eth import EthereumClient

from safe_transaction_service.contracts.models import Contract
from safe_transaction_service.contracts.tests.factories import ContractFactory


class TestCommands(TestCase):
    def test_index_contracts_with_metadata(self):
        command = "index_contracts_with_metadata"

        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn(
            "Calling `create_missing_contracts_with_metadata_task` task", buf.getvalue()
        )
        self.assertIn("Task was sent", buf.getvalue())

        buf = StringIO()
        call_command(command, "--reindex", "--sync", stdout=buf)
        self.assertIn(
            "Calling `reindex_contracts_without_metadata_task` task", buf.getvalue()
        )
        self.assertIn("Processing finished", buf.getvalue())

    @patch.object(EthereumClient, "get_chain_id", autospec=True, return_value=137)
    def test_setup_safe_contracts(self, mock_chain_id: MagicMock):
        command = "setup_safe_contracts"
        buf = StringIO()
        random_contract = ContractFactory()
        previous_random_contract_logo = random_contract.logo.read()
        multisend_address = "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"
        multisend_contract = ContractFactory(
            address=multisend_address, name="GnosisMultisend"
        )
        multisend_contract_logo = multisend_contract.logo.read()

        call_command(command, stdout=buf)
        current_multisend_contract = Contract.objects.get(address=multisend_address)
        # Previous created contracts logo should be updated
        self.assertNotEqual(
            current_multisend_contract.logo.read(), multisend_contract_logo
        )

        # Previous created contracts name and display name should keep unchanged
        self.assertEqual(multisend_contract.name, current_multisend_contract.name)
        self.assertEqual(
            multisend_contract.display_name, current_multisend_contract.display_name
        )

        # No safe contract logos should keep unchanged
        current_no_safe_contract_logo: bytes = Contract.objects.get(
            address=random_contract.address
        ).logo.read()
        self.assertEqual(current_no_safe_contract_logo, previous_random_contract_logo)

        # Missing safe addresses should be added
        self.assertEqual(Contract.objects.count(), 31)

        # Contract name and display name should be correctly generated
        safe_l2_130_address = "0x3E5c63644E683549055b9Be8653de26E0B4CD36E"
        contract = Contract.objects.get(address=safe_l2_130_address)
        self.assertEqual(contract.name, "GnosisSafeL2")
        self.assertEqual(contract.display_name, "SafeL2 1.3.0")
        self.assertFalse(contract.trusted_for_delegate_call)

        safe_multisend_130_address = "0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761"
        contract = Contract.objects.get(address=safe_multisend_130_address)
        self.assertEqual(contract.name, "MultiSend")
        self.assertEqual(contract.display_name, "Safe: MultiSend 1.3.0")

        # Force to update contract names should update the name and display name of the contract
        call_command(
            command,
            "--force-update-contracts",
            stdout=buf,
        )
        contract = Contract.objects.get(address=multisend_address)
        self.assertEqual(contract.name, "MultiSendCallOnly")
        self.assertEqual(contract.display_name, "Safe: MultiSendCallOnly 1.3.0")
        # MultiSendCallOnly should be trusted for delegate calls
        self.assertTrue(contract.trusted_for_delegate_call)

        multisend_141_address = "0x9641d764fc13c8B624c04430C7356C1C7C8102e2"
        contract = Contract.objects.get(address=multisend_141_address)
        self.assertEqual(contract.name, "MultiSendCallOnly")
        self.assertEqual(contract.display_name, "Safe: MultiSendCallOnly 1.4.1")
        # MultiSendCallOnly should be trusted for delegate calls
        self.assertTrue(contract.trusted_for_delegate_call)

        safe_to_l2_migration = "0xfF83F6335d8930cBad1c0D439A841f01888D9f69"
        contract = Contract.objects.get(address=safe_to_l2_migration)
        self.assertEqual(contract.name, "SafeToL2Migration")
        self.assertEqual(contract.display_name, "SafeToL2Migration 1.4.1")
        # SafeToL2Migration should be untrusted for delegate calls
        self.assertFalse(contract.trusted_for_delegate_call)

        sign_message_lib = "0xd53cd0aB83D845Ac265BE939c57F53AD838012c9"
        contract = Contract.objects.get(address=sign_message_lib)
        self.assertEqual(contract.name, "SignMessageLib")
        self.assertEqual(contract.display_name, "Safe: SignMessageLib 1.4.1")
        # SignMessageLib should be trusted for delegate calls
        self.assertTrue(contract.trusted_for_delegate_call)

    @patch(
        "safe_transaction_service.contracts.management.commands.setup_safe_contracts.EthereumClient.is_contract"
    )
    @patch.object(EthereumClient, "get_chain_id", autospec=True, return_value=2)
    def test_setup_safe_contracts_from_chain(
        self, mock_chain_id: MagicMock, mock_is_contract: MagicMock
    ):
        command = "setup_safe_contracts"
        buf = StringIO()
        mock_is_contract.return_value = False
        self.assertEqual(Contract.objects.count(), 0)
        call_command(command, stdout=buf)
        self.assertEqual(Contract.objects.count(), 0)

        # Mock is contract to return True in case of provided address is equal to MultiSend v1.4.1 address
        mulsisend_address = "0x38869bf66a61cF6bDB996A6aE40D5853Fd43B526"
        mock_is_contract.side_effect = lambda contract_address: (
            True if contract_address == mulsisend_address else False
        )
        call_command(command, stdout=buf)
        self.assertEqual(Contract.objects.count(), 1)
        contract = Contract.objects.get(address=mulsisend_address)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.name, "MultiSend")
        self.assertEqual(contract.display_name, "Safe: MultiSend 1.4.1")
