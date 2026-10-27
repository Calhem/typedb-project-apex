"""Apex settings, read from environment variables (and a local .env file, if present).

Every setting is checked when it is loaded, so a mistake stops the program straight away with a
message that names the setting. The password is kept as SecretStr, so it never appears in logs.
"""

import re

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ADDRESS = re.compile(r"^[A-Za-z0-9.\-]+:\d{1,5}$")

# The databases Apex is allowed to create, replace or delete. Anything else on the server is
# never touched. Tests use throwaway apex-test-* databases.
PRISTINE_DATABASE = "apex"
DEMO_DATABASE = "apex-demo"
TEST_PREFIX = "apex-test-"


class ConfigError(Exception):
    """The configuration is missing or invalid. The message says what to change."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    typedb_address: str = Field("127.0.0.1:1732", description="host:port of Apex's TypeDB server")
    typedb_username: str = Field("admin", min_length=1)
    typedb_password: SecretStr = SecretStr("password")
    apex_database: str = Field(PRISTINE_DATABASE, description="the database the app reads")

    @field_validator("typedb_address")
    @classmethod
    def _address(cls, value):
        if not _ADDRESS.match(value) or not 0 < int(value.rsplit(":", 1)[1]) < 65536:
            raise ValueError("must look like host:port, for example 127.0.0.1:1732")
        return value

    @field_validator("apex_database")
    @classmethod
    def _database(cls, value):
        if not is_apex_database(value):
            raise ValueError(f"must be {PRISTINE_DATABASE!r}, {DEMO_DATABASE!r} or {TEST_PREFIX}...")
        return value


def is_apex_database(name):
    """True only for the database names Apex owns."""
    return name in (PRISTINE_DATABASE, DEMO_DATABASE) or bool(
        re.fullmatch(re.escape(TEST_PREFIX) + r"[a-z0-9-]{1,40}", name))


def load_settings(**overrides):
    try:
        return Settings(**overrides)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in error.errors())
        raise ConfigError(f"Invalid settings ({problems}). Check your .env file (see .env.example).") from None
