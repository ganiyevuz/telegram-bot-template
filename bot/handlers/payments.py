# ruff: noqa: TC001, TC002  - aiogram, dishka and FastAPI resolve these handler
# signatures at runtime via get_type_hints(); these imports must stay at module level
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.callback_data import MenuCB
from bot.services.payments import PaymentService

router = Router(name="payments")


@router.callback_query(MenuCB.filter(F.action == "premium"))
async def send_premium_invoice(
    query: CallbackQuery,
    bot: FromDishka[Bot],
    payments: FromDishka[PaymentService],
    settings: FromDishka[Settings],
) -> None:
    # No explicit `query.answer()` here: `CallbackAnswerMiddleware` (registered in
    # bot/middlewares/__init__.py) always answers in its `finally` block, so calling
    # `query.answer()` directly would trigger a second, blank answer that can clobber
    # this one on the client — see how callbacks.py handles the same constraint.
    # Sending the invoice is itself the user-visible feedback for the tap.
    # Stars only. Switching PAYMENT_CURRENCY to a fiat currency would need more than a
    # setting: a `provider_token` from a Telegram-approved payment provider, amounts in
    # minor units (`premium_price` * 100 for a 2-decimal currency), and the fiat-only
    # invoice fields (need_email / need_shipping_address / max_tip_amount / ...). None of
    # that is implemented here, so no half-working knob is exposed for it.
    await bot.send_invoice(
        chat_id=query.from_user.id,
        title=_("Premium access"),
        description=_("Unlock premium features for {days} days").format(
            days=settings.payments.subscription_period_days,
        ),
        payload=payments.build_payload(query.from_user.id),
        currency=settings.payments.currency,
        prices=[LabeledPrice(label=_("Premium"), amount=settings.payments.premium_price)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, payments: FromDishka[PaymentService]) -> None:
    ok, reason = await payments.validate(
        query.invoice_payload,
        query.from_user.id,
        query.total_amount,
        query.currency,
    )
    await query.answer(ok=ok, error_message=reason)


@router.message(F.successful_payment)
async def payment_succeeded(message: Message, payments: FromDishka[PaymentService]) -> None:
    # `F.successful_payment` guarantees this at runtime but doesn't narrow the type for
    # mypy, and `Message.from_user` is `None` for channel posts - guard both explicitly.
    if message.from_user is None or message.successful_payment is None:
        return
    payment = message.successful_payment
    credited = await payments.record(message.from_user.id, payment)
    if not credited:
        return
    # A renewal arrives as another `successful_payment` (is_recurring=True), but only
    # the very first one of a subscription is `is_first_recurring` - telling the user
    # "premium is active" every month as though it were new would be misleading.
    if payment.is_recurring and not payment.is_first_recurring:
        await message.answer(_("subscription renewed, premium is active"))
    else:
        await message.answer(_("payment received, premium is active"))
