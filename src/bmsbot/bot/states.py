from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    waiting_pin = State()
    choosing_region = State()
    choosing_movie = State()
    choosing_alerts = State()


class SettingsFlow(StatesGroup):
    waiting_region = State()
