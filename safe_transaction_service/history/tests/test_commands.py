import os.path
import tempfile
from io import StringIO
from unittest import mock
from unittest.mock import MagicMock, PropertyMock

from django.core.management import CommandError, call_command
from django.test import TestCase

from django_celery_beat.models import PeriodicTask
from eth_account import Account
from safe_eth.eth.account_abstraction import BundlerClient
from safe_eth.eth.ethereum_client import EthereumClient, EthereumNetwork
from safe_eth.safe import Safe
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from safe_eth.util.util import to_0x_hex_str

from ..indexers import Erc20EventsIndexer, InternalTxIndexer, SafeEventsIndexer
from ..models import (
    IndexingStatus,
    InternalTxDecoded,
    ProxyFactory,
    SafeLastStatus,
    SafeMasterCopy,
)
from ..services import IndexServiceProvider
from ..tasks import logger as task_logger
from .factories import (
    MultisigConfirmationFactory,
    MultisigTransactionFactory,
    SafeContractFactory,
    SafeLastStatusFactory,
    SafeMasterCopyFactory,
)


class TestCommands(SafeTestCaseMixin, TestCase):
    @mock.patch.object(EthereumClient, "get_network", autospec=True)
    def _test_setup_service(
        self,
        ethereum_network: EthereumNetwork,
        ethereum_client_get_network_mock: MagicMock,
    ):
        command = "setup_service"
        ethereum_client_get_network_mock.return_value = ethereum_network
        buf = StringIO()
        self.assertEqual(SafeMasterCopy.objects.count(), 0)
        self.assertEqual(ProxyFactory.objects.count(), 0)
        self.assertEqual(PeriodicTask.objects.count(), 0)

        call_command(command, stdout=buf)
        self.assertIn(
            f"Setting up {ethereum_network.name} safe addresses", buf.getvalue()
        )
        self.assertIn(
            f"Setting up {ethereum_network.name} proxy factory addresses",
            buf.getvalue(),
        )
        self.assertIn("Created Periodic Task", buf.getvalue())
        self.assertNotIn("was already created", buf.getvalue())
        self.assertGreater(SafeMasterCopy.objects.count(), 0)
        self.assertGreater(ProxyFactory.objects.count(), 0)
        self.assertGreater(PeriodicTask.objects.count(), 0)

        # Check last master copy was created
        last_master_copy_address = "0x41675C099F32341bf84BFc5382aF534df5C7461a"
        last_master_copy = SafeMasterCopy.objects.get(address=last_master_copy_address)
        self.assertGreater(last_master_copy.initial_block_number, 0)
        self.assertGreater(last_master_copy.tx_block_number, 0)

        # Check last proxy factory was created
        last_proxy_factory_address = "0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67"
        last_proxy_factory = ProxyFactory.objects.get(
            address=last_proxy_factory_address
        )
        self.assertGreater(last_proxy_factory.initial_block_number, 0)
        self.assertGreater(last_proxy_factory.tx_block_number, 0)

        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn(
            f"Setting up {ethereum_network.name} safe addresses", buf.getvalue()
        )
        self.assertIn(
            f"Setting up {ethereum_network.name} proxy factory addresses",
            buf.getvalue(),
        )
        self.assertIn("Removing old tasks", buf.getvalue())
        self.assertIn("Old tasks were removed", buf.getvalue())
        self.assertIn("Created Periodic Task", buf.getvalue())

    def test_index_erc20(self):
        command = "index_erc20"
        buf = StringIO()
        with self.assertLogs(logger=task_logger) as cm:
            call_command(command, stdout=buf)
            self.assertIn("No addresses to process", cm.output[0])

        buf = StringIO()
        call_command(command, "--block-process-limit=10", stdout=buf)
        self.assertIn("Setting block-process-limit to 10", buf.getvalue())

        buf = StringIO()
        call_command(
            command,
            "--block-process-limit=10",
            "--block-process-limit-max=15",
            stdout=buf,
        )
        self.assertIn("Setting block-process-limit to 10", buf.getvalue())
        self.assertIn("Setting block-process-limit-max to 15", buf.getvalue())

        with self.assertLogs(logger=task_logger) as cm:
            safe_contract = SafeContractFactory()
            addresses = {safe_contract.address}
            buf = StringIO()
            call_command(command, stdout=buf)
            self.assertIn(
                f"Start indexing of erc20/721 events for out of sync addresses {addresses}",
                cm.output[0],
            )
            self.assertIn(
                "Indexing of erc20/721 events for out of sync addresses task processed 0 events",
                cm.output[1],
            )

        with self.assertLogs(logger=task_logger) as cm:
            safe_contract_2 = SafeContractFactory()
            addresses = {safe_contract_2.address}
            buf = StringIO()
            call_command(command, f"--addresses={safe_contract_2.address}", stdout=buf)
            self.assertIn(
                f"Start indexing of erc20/721 events for out of sync addresses {addresses}",
                cm.output[0],
            )
            self.assertIn(
                "Indexing of erc20/721 events for out of sync addresses task processed 0 events",
                cm.output[1],
            )

        # Test sync task call
        with self.assertLogs(logger=task_logger) as cm:
            safe_contract_2 = SafeContractFactory()
            addresses = {safe_contract_2.address}
            buf = StringIO()
            call_command(
                command, f"--addresses={safe_contract_2.address}", "--sync", stdout=buf
            )
            self.assertIn(
                f"Start indexing of erc20/721 events for out of sync addresses {addresses}",
                cm.output[0],
            )
            self.assertIn(
                "Indexing of erc20/721 events for out of sync addresses task processed 0 events",
                cm.output[1],
            )

    @mock.patch.object(
        EthereumClient, "current_block_number", new_callable=PropertyMock
    )
    def test_reindex_master_copies(self, current_block_number_mock: PropertyMock):
        logger_name = "safe_transaction_service.history.services.index_service"
        current_block_number_mock.return_value = 1000
        command = "reindex_master_copies"

        with self.assertRaisesMessage(
            CommandError, "the following arguments are required: --from-block-number"
        ):
            call_command(command)

        buf = StringIO()
        with self.assertLogs(logger_name, level="WARNING") as cm:
            call_command(
                command,
                "--block-process-limit=11",
                "--from-block-number=76",
                stdout=buf,
            )
            self.assertIn("Setting block-process-limit to 11", buf.getvalue())
            self.assertIn("Setting from-block-number to 76", buf.getvalue())
            self.assertIn("No addresses to process", cm.output[0])

        safe_master_copy = SafeMasterCopyFactory(l2=False)
        buf = StringIO()
        with self.assertLogs(logger_name, level="INFO") as cm:
            with mock.patch.object(
                InternalTxIndexer, "find_relevant_elements", return_value=[]
            ) as find_relevant_elements_mock:
                IndexServiceProvider.del_singleton()
                from_block_number = 100
                block_process_limit = 500
                call_command(
                    command,
                    f"--block-process-limit={block_process_limit}",
                    f"--from-block-number={from_block_number}",
                    stdout=buf,
                )
                expected_addresses = {safe_master_copy.address}
                self.assertIn(
                    f"Start reindexing addresses {expected_addresses}",
                    cm.output[0],
                )
                self.assertIn("found 0 traces/events", cm.output[1])
                self.assertIn(
                    f"End reindexing addresses {expected_addresses}",
                    cm.output[3],
                )
                find_relevant_elements_mock.assert_any_call(
                    {safe_master_copy.address},
                    from_block_number,
                    from_block_number + block_process_limit - 1,
                )
                find_relevant_elements_mock.assert_any_call(
                    {safe_master_copy.address},
                    from_block_number + block_process_limit,
                    current_block_number_mock.return_value,
                )
                self.assertEqual(find_relevant_elements_mock.call_count, 2)

        with self.settings(ETH_L2_NETWORK=True):
            IndexServiceProvider.del_singleton()
            buf = StringIO()
            with self.assertLogs(logger_name, level="WARNING") as cm:
                call_command(command, "--from-block-number=71", stdout=buf)
            self.assertIn("No addresses to process", cm.output[0])

            with self.assertLogs(logger_name, level="INFO") as cm:
                with mock.patch.object(
                    SafeEventsIndexer, "find_relevant_elements", return_value=[]
                ) as find_relevant_elements_mock:
                    safe_l2_master_copy = SafeMasterCopyFactory(l2=True)
                    buf = StringIO()
                    from_block_number = 200
                    block_process_limit = 500
                    call_command(
                        command,
                        f"--block-process-limit={block_process_limit}",
                        f"--from-block-number={from_block_number}",
                        stdout=buf,
                    )
                    expected_addresses = {safe_l2_master_copy.address}
                    self.assertIn(
                        f"Start reindexing addresses {expected_addresses}",
                        cm.output[0],
                    )
                    self.assertIn("found 0 traces/events", cm.output[1])
                    self.assertIn(
                        f"End reindexing addresses {expected_addresses}",
                        cm.output[3],
                    )
                    find_relevant_elements_mock.assert_any_call(
                        {safe_l2_master_copy.address},
                        from_block_number,
                        from_block_number + block_process_limit - 1,
                    )
                    find_relevant_elements_mock.assert_any_call(
                        {safe_l2_master_copy.address},
                        from_block_number + block_process_limit,
                        current_block_number_mock.return_value,
                    )
                    self.assertEqual(find_relevant_elements_mock.call_count, 2)
        IndexServiceProvider.del_singleton()

    @mock.patch.object(
        EthereumClient, "current_block_number", new_callable=PropertyMock
    )
    def test_reindex_erc20_events(self, current_block_number_mock: PropertyMock):
        logger_name = "safe_transaction_service.history.services.index_service"
        current_block_number_mock.return_value = 1000
        command = "reindex_erc20"

        with self.assertRaisesMessage(
            CommandError, "the following arguments are required: --from-block-number"
        ):
            call_command(command)

        buf = StringIO()
        with self.assertLogs(logger_name, level="WARNING") as cm:
            call_command(
                command,
                "--block-process-limit=11",
                "--from-block-number=76",
                stdout=buf,
            )
            self.assertIn("Setting block-process-limit to 11", buf.getvalue())
            self.assertIn("Setting from-block-number to 76", buf.getvalue())
            self.assertIn("No addresses to process", cm.output[0])

        safe_contract = SafeContractFactory()
        buf = StringIO()
        with self.assertLogs(logger_name, level="INFO") as cm:
            with mock.patch.object(
                Erc20EventsIndexer, "find_relevant_elements", return_value=[]
            ) as find_relevant_elements_mock:
                IndexServiceProvider.del_singleton()
                from_block_number = 100
                block_process_limit = 500
                call_command(
                    command,
                    f"--block-process-limit={block_process_limit}",
                    f"--from-block-number={from_block_number}",
                    stdout=buf,
                )
                expected_addresses = {safe_contract.address}
                self.assertIn(
                    f"Start reindexing addresses {expected_addresses}",
                    cm.output[0],
                )
                self.assertIn("found 0 traces/events", cm.output[1])
                self.assertIn(
                    f"End reindexing addresses {expected_addresses}",
                    cm.output[3],
                )
                find_relevant_elements_mock.assert_any_call(
                    {safe_contract.address},
                    from_block_number,
                    from_block_number + block_process_limit - 1,
                )
                find_relevant_elements_mock.assert_any_call(
                    {safe_contract.address},
                    from_block_number + block_process_limit,
                    current_block_number_mock.return_value,
                )
                self.assertEqual(find_relevant_elements_mock.call_count, 2)
        IndexServiceProvider.del_singleton()

    def test_setup_service_mainnet(self):
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number, 0
        )
        self._test_setup_service(EthereumNetwork.MAINNET)
        first_safe_block_deployed = (
            6569433  # 0.0.2 deployment block, first Safe contract
        )
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number,
            first_safe_block_deployed,
        )

        # Check last master copy was created
        last_master_copy_address = "0x6851D6fDFAfD08c0295C392436245E5bc78B0185"
        last_master_copy_initial_block = 10329734
        last_master_copy = SafeMasterCopy.objects.get(address=last_master_copy_address)
        self.assertEqual(
            last_master_copy.initial_block_number, last_master_copy_initial_block
        )
        self.assertEqual(
            last_master_copy.tx_block_number, last_master_copy_initial_block
        )

        # Check last proxy factory was created
        last_proxy_factory_address = "0x76E2cFc1F5Fa8F6a5b3fC4c8F4788F0116861F9B"
        last_proxy_factory_initial_block = 9084508
        last_proxy_factory = ProxyFactory.objects.get(
            address=last_proxy_factory_address
        )
        self.assertEqual(
            last_proxy_factory.initial_block_number, last_proxy_factory_initial_block
        )
        self.assertEqual(
            last_proxy_factory.tx_block_number, last_proxy_factory_initial_block
        )

        # At Nov 2023 we support 12 Master Copies, 3 L2 Master Copies and 6 Proxy Factories
        self.assertEqual(SafeMasterCopy.objects.count(), 12)
        self.assertEqual(SafeMasterCopy.objects.l2().count(), 3)
        self.assertEqual(ProxyFactory.objects.count(), 6)

    def test_setup_service_mainnet_erc20_indexing_setup(self):
        # Test IndexingStatus ERC20 is not modified if higher than the oldest master copy
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number, 0
        )
        first_safe_block_deployed = (
            6569433  # 0.0.2 deployment block, first Safe contract
        )
        IndexingStatus.objects.set_erc20_721_indexing_status(
            first_safe_block_deployed + 20
        )
        self._test_setup_service(EthereumNetwork.MAINNET)
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number,
            first_safe_block_deployed + 20,
        )

    def test_setup_service_sepolia(self):
        self._test_setup_service(EthereumNetwork.SEPOLIA)

    @mock.patch.object(EthereumClient, "is_contract", return_value=False)
    @mock.patch.object(EthereumClient, "get_network", autospec=True)
    def test_setup_service_not_valid_network(
        self, ethereum_client_get_network_mock: MagicMock, is_contract_mock: MagicMock
    ):
        command = "setup_service"
        for return_value in (EthereumNetwork.ROPSTEN, EthereumNetwork.UNKNOWN):
            ethereum_client_get_network_mock.return_value = return_value
            buf = StringIO()
            call_command(command, stdout=buf)
            self.assertIn(
                "Cannot find any SafeMasterCopy and ProxyFactory for chain id",
                buf.getvalue(),
            )

    @mock.patch.object(EthereumClient, "get_network", autospec=True)
    def test_setup_service_without_addresses(
        self, ethereum_client_get_network_mock: MagicMock
    ):
        """
        Test setup_service without any SafeSingleton and ProxyFactory added on safe-eth-py.
        Setup service command should check if any of the default addresses is a contract on chain and add them.
        """
        command = "setup_service"
        ethereum_network = EthereumNetwork.GANACHE
        ethereum_client_get_network_mock.return_value = ethereum_network
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertNotIn(
            "Cannot find any SafeMasterCopy and ProxyFactory for chain id",
            buf.getvalue(),
        )
        self.assertIn(
            f"Setting up {ethereum_network.name} default safe addresses from chain with unknown init block",
            buf.getvalue(),
        )
        self.assertEqual(SafeMasterCopy.objects.count(), 2)
        self.assertEqual(ProxyFactory.objects.count(), 1)

    @mock.patch.object(EthereumClient, "get_network", autospec=True)
    def test_setup_service_update_singletons(
        self, ethereum_client_get_network_mock: MagicMock
    ):
        """
        Test if version or initial block_number is being updated when safe_eth_py contains different version or
        intial_block_number than the database values.
        """
        command = "setup_service"
        ethereum_network = EthereumNetwork.GANACHE
        ethereum_client_get_network_mock.return_value = ethereum_network
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertEqual(SafeMasterCopy.objects.count(), 2)
        self.assertEqual(ProxyFactory.objects.count(), 1)
        master_copy = SafeMasterCopy.objects.first()
        self.assertEqual(master_copy.initial_block_number, 0)
        mocked_addresses = {ethereum_network: [(master_copy.address, 111, "v1.1.1")]}

        with mock.patch(
            "safe_transaction_service.history.management.commands.setup_service.MASTER_COPIES",
            new=mocked_addresses,
        ):
            buf = StringIO()
            call_command(command, stdout=buf)
            master_copy_updated = SafeMasterCopy.objects.get(
                address=master_copy.address
            )
            self.assertEqual(master_copy_updated.initial_block_number, 111)
            self.assertEqual(master_copy_updated.version, "v1.1.1")

    def test_export_multisig_tx_data(self):
        with tempfile.TemporaryDirectory() as tmpdirname:
            command = "export_multisig_tx_data"
            arguments = "--file-name=" + os.path.join(tmpdirname, "result.csv")
            buf = StringIO()
            call_command(command, arguments, stdout=buf)
            self.assertIn("Start exporting of 0", buf.getvalue())

            MultisigTransactionFactory(origin="something")
            MultisigTransactionFactory(
                origin="another-something", ethereum_tx=None
            )  # Will not be exported
            MultisigTransactionFactory(origin={})  # Will not be exported
            buf = StringIO()
            call_command(command, arguments, stdout=buf)
            self.assertIn("Start exporting of 1", buf.getvalue())

    @mock.patch(
        "safe_transaction_service.history.management.commands.check_chainid_matches.get_bundler_client",
        return_value=None,
    )
    @mock.patch(
        "safe_transaction_service.history.management.commands.check_chainid_matches.get_chain_id"
    )
    def test_check_chainid_matches(
        self, get_chain_id_mock: MagicMock, get_bundler_client_mock: MagicMock
    ):
        command = "check_chainid_matches"

        # Create ChainId model
        get_chain_id_mock.return_value = EthereumNetwork.MAINNET.value
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn("EthereumRPC chainId 1 looks good", buf.getvalue())

        # Use different chainId
        get_chain_id_mock.return_value = EthereumNetwork.GNOSIS.value
        with self.assertRaisesMessage(
            CommandError,
            "EthereumRPC chainId 100 does not match previously used chainId 1",
        ):
            call_command(command)

        # Check again with the initial chainId
        get_chain_id_mock.return_value = EthereumNetwork.MAINNET.value
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn("EthereumRPC chainId 1 looks good", buf.getvalue())

    @mock.patch.object(BundlerClient, "get_chain_id", return_value=1234)
    @mock.patch(
        "safe_transaction_service.history.management.commands.check_chainid_matches.get_bundler_client",
        return_value=BundlerClient(""),
    )
    @mock.patch(
        "safe_transaction_service.history.management.commands.check_chainid_matches.get_chain_id",
        return_value=EthereumNetwork.MAINNET.value,
    )
    def test_check_chainid_bundler_matches(
        self,
        get_chain_id_mock: MagicMock,
        get_bundler_client_mock: MagicMock,
        bundler_get_chain_id_mock: MagicMock,
    ):
        command = "check_chainid_matches"
        with self.assertRaisesMessage(
            CommandError,
            "ERC4337 BundlerClient chainId 1234 does not match EthereumClient chainId 1",
        ):
            call_command(command)

        bundler_get_chain_id_mock.return_value = EthereumNetwork.MAINNET.value
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertEqual(
            "EthereumRPC chainId 1 looks good\nERC4337 BundlerClient chainId 1 looks good\n",
            buf.getvalue(),
        )

    @mock.patch(
        "safe_transaction_service.history.management.commands.check_index_problems.settings.ETH_L2_NETWORK",
        return_value=True,
    )  # Testing L2 chain as ganache haven't tracing methods
    def test_check_index_problems(self, mock_eth_l2_network: MagicMock):
        command = "check_index_problems"
        buf = StringIO()
        # Test empty with empty SafeContract model
        call_command(command, stdout=buf)
        self.assertIn("Database haven't any address to be checked", buf.getvalue())

        # Should ignore Safe with nonce 0
        owner = Account.create()
        safe = self.deploy_test_safe(
            number_owners=1,
            threshold=1,
            owners=[owner.address],
            initial_funding_wei=1000,
        )
        SafeContractFactory(address=safe.address)
        SafeLastStatusFactory(nonce=0, address=safe.address)
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn("Database haven't any address to be checked", buf.getvalue())

        # Should detect missing transactions
        data = b""
        value = 122
        to = Account.create().address
        multisig_tx = safe.build_multisig_tx(to, value, data)
        multisig_tx.sign(owner.key)
        tx_hash, _ = multisig_tx.execute(self.ethereum_test_account.key)
        SafeLastStatus.objects.filter(address=safe.address).update(nonce=1)
        self.assertEqual(InternalTxDecoded.objects.count(), 0)
        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn(
            f"Safe={safe.address} is corrupted, has some old transactions missing",
            buf.getvalue(),
        )
        self.assertEqual(InternalTxDecoded.objects.count(), 1)
        with self.assertRaises(SafeLastStatus.DoesNotExist):
            SafeLastStatus.objects.get(address=safe.address)

        # Should works with batch_size option
        SafeLastStatusFactory(nonce=1, address=safe.address)
        buf = StringIO()
        call_command(command, "--batch-size=1", stdout=buf)
        self.assertIn(
            f"Safe={safe.address} is corrupted, has some old transactions missing",
            buf.getvalue(),
        )
        self.assertEqual(InternalTxDecoded.objects.count(), 1)
        with self.assertRaises(SafeLastStatus.DoesNotExist):
            SafeLastStatus.objects.get(address=safe.address)

        # Should detect incorrect nonce
        with mock.patch.object(SafeLastStatus, "is_corrupted", return_value=False):
            SafeLastStatusFactory(nonce=2, address=safe.address)
            buf = StringIO()
            call_command(command, stdout=buf)
            self.assertIn(
                f"Safe={safe.address} stored nonce=2 is different from blockchain-nonce=1",
                buf.getvalue(),
            )
            with self.assertRaises(SafeLastStatus.DoesNotExist):
                SafeLastStatus.objects.get(address=safe.address)

    def test_validate_tx_integrity(self):
        command = "validate_tx_integrity"

        buf = StringIO()
        call_command(command, stdout=buf)
        self.assertIn("Found 0 transactions", buf.getvalue())

        account = Account.create()
        safe_last_status = SafeLastStatusFactory(nonce=0, address=account.address)
        multisig_transaction = MultisigTransactionFactory(
            ethereum_tx=None, nonce=0, safe=safe_last_status.address
        )
        multisig_confirmation = MultisigConfirmationFactory(
            multisig_transaction=multisig_transaction
        )

        # Signatures are not valid by default
        buf = StringIO()
        call_command(command, stdout=buf)
        text = buf.getvalue()
        self.assertIn("Found 1 transactions", text)
        self.assertNotIn("should not have signatures as it is not executed", text)
        self.assertIn(f"{multisig_transaction.safe_tx_hash} is not matching", text)
        self.assertIn(
            f"Confirmation for owner {multisig_confirmation.owner} is not valid for multisig transaction {multisig_transaction.safe_tx_hash}",
            text,
        )

        # Fix confirmation signature
        multisig_confirmation.owner = account.address
        multisig_confirmation.signature = account.unsafe_sign_hash(
            multisig_transaction.safe_tx_hash
        )["signature"]
        multisig_confirmation.save(update_fields=["owner", "signature"])

        # Confirmation signature is now alright
        buf = StringIO()
        call_command(command, stdout=buf)
        text = buf.getvalue()
        self.assertIn("Found 1 transactions", text)
        self.assertNotIn("should not have signatures as it is not executed", text)
        self.assertIn(f"{multisig_transaction.safe_tx_hash} is not matching", text)
        self.assertNotIn("is not valid for multisig transaction", text)

        # Fix safeTxHash too
        safe = Safe(multisig_transaction.safe, self.ethereum_client)
        safe_tx = safe.build_multisig_tx(
            multisig_transaction.to,
            multisig_transaction.value,
            multisig_transaction.data,
            multisig_transaction.operation,
            multisig_transaction.safe_tx_gas,
            multisig_transaction.base_gas,
            multisig_transaction.gas_price,
            multisig_transaction.gas_token,
            multisig_transaction.refund_receiver,
            safe_nonce=multisig_transaction.nonce,
        )
        multisig_transaction.delete()  # When replacing primary key, a new instance will be created
        multisig_transaction.safe_tx_hash = to_0x_hex_str(safe_tx.safe_tx_hash)
        multisig_transaction.save()

        # SafeTxHash is good now
        buf = StringIO()
        call_command(command, stdout=buf)
        text = buf.getvalue()
        self.assertIn("Found 1 transactions", text)
        self.assertNotIn("should not have signatures as it is not executed", text)
        self.assertNotIn("is not matching", text)
        self.assertNotIn("is not valid for multisig transaction", text)

        # Empty signatures
        multisig_transaction.signatures = b"123456"
        multisig_transaction.save(update_fields=["signatures"])

        # Test signature not empty
        buf = StringIO()
        call_command(command, stdout=buf)
        text = buf.getvalue()
        self.assertIn("Found 1 transactions", text)
        self.assertIn(
            f"{multisig_transaction.safe_tx_hash} should not have signatures as it is not executed",
            text,
        )
        self.assertNotIn("is not matching", text)
        self.assertNotIn("is not valid for multisig transaction", text)
