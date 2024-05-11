import asyncio
from typing import AsyncGenerator, Dict, Optional

from lightspark import (
    Invoice,
    LightsparkNode,
    LightsparkSyncClient,
    OutgoingPayment,
    PaymentRequestStatus,
    TransactionStatus,
)
from lightspark.utils.currency_amount import amount_as_msats
from loguru import logger

from lnbits.settings import settings

from .base import (
    InvoiceResponse,
    PaymentPendingStatus,
    PaymentResponse,
    PaymentStatus,
    StatusResponse,
    Wallet,
)


class LightsparkWallet(Wallet):
    """https://docs.lightspark.com/lightspark-sdk/getting-started"""

    def __init__(self):
        if not settings.lightspark_api_endpoint:
            raise ValueError(
                "cannot initialize LightsparkWallet: missing lightspark_api_endpoint"
            )
        if not settings.lightsaprk_api_token_id:
            raise ValueError(
                "cannot initialize LightsparkWallet: missing lightspark_access_token"
            )
        if not settings.lightsaprk_api_token_secret:
            raise ValueError(
                "cannot initialize LightsparkWallet: missing lightsaprk_api_token_secret"
            )
        if not settings.lightspark_node_id:
            raise ValueError(
                "cannot initialize LightsparkWallet: missing lightspark_node_id"
            )
        if not settings.lightspark_node_password:
            raise ValueError(
                "cannot initialize LightsparkWallet: missing lightspark_node_password"
            )

        self.endpoint = self.normalize_endpoint(settings.lightspark_api_endpoint)
        self.client = LightsparkSyncClient(
            settings.lightsaprk_api_token_id,
            settings.lightsaprk_api_token_secret,
            self.endpoint,
        )
        self.client.recover_node_signing_key(
            settings.lightspark_node_id, settings.lightspark_node_password
        )
        self.node_id = settings.lightspark_node_id

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

    async def status(self) -> StatusResponse:
        try:
            node = self.client.get_entity(self.node_id, LightsparkNode)
            if node is None:
                return StatusResponse("Node not found", 0)
            if node.balances is None:
                return StatusResponse("Balances not found", 0)

            return StatusResponse(None, amount_as_msats(node.balances.owned_balance))
        except Exception as exc:
            logger.warning(exc)
            return StatusResponse(f"Unable to connect to {self.endpoint}.", 0)

    async def create_invoice(
        self,
        amount: int,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        **kwargs,
    ) -> InvoiceResponse:
        # TODO: Support description_hash and unhashed_description in Lightspark API.
        try:
            invoice = self.client.create_invoice(self.node_id, amount, memo)
            return InvoiceResponse(
                True, invoice.id, invoice.data.encoded_payment_request, None
            )
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(False, None, None, f"Failed to create invoice.")

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        try:
            payment = self.client.pay_invoice(self.node_id, bolt11, 60, fee_limit_msat)
            return PaymentResponse(
                payment.status == TransactionStatus.SUCCESS,
                payment.id,
                amount_as_msats(payment.fees) if payment.fees is not None else None,
                payment.payment_preimage,
                (
                    None
                    if payment.status != TransactionStatus.FAILED
                    else payment.failure_message
                ),
            )
        except Exception as exc:
            logger.info(f"Failed to pay invoice {bolt11}")
            logger.warning(exc)
            return PaymentResponse(
                None, None, None, None, f"Unable to connect to {self.endpoint}."
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        try:
            invoice = self.client.get_entity(checking_id, Invoice)
            if invoice is None:
                return PaymentPendingStatus()

            return PaymentStatus(
                invoice.status == PaymentRequestStatus.CLOSED
                and invoice.amount_paid is not None
                and invoice.amount_paid > 0,
                # Note: The Lightspark SDK only exposes fees and preimage for outgoing payments.
                None,
                None,
            )
        except Exception as exc:
            logger.error("Error getting invoice status: {exc}")
            return PaymentPendingStatus()

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        try:
            payment = self.client.get_entity(checking_id, OutgoingPayment)
            if payment is None:
                return PaymentPendingStatus()

            return PaymentStatus(
                payment.status == TransactionStatus.SUCCESS,
                amount_as_msats(payment.fees) if payment.fees is not None else None,
                payment.payment_preimage,
            )
        except Exception as e:
            logger.error(f"Error getting payment status: {e}")
            return PaymentPendingStatus()

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        self.queue: asyncio.Queue = asyncio.Queue(0)
        while settings.lnbits_running:
            value = await self.queue.get()
            yield value
