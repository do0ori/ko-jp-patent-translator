import unittest
from unittest.mock import patch

from utils import translation


class ClientApiVersionTests(unittest.TestCase):
    """The app depends on JSON mode (response_mime_type / response_schema).

    The Gemini Developer API only serves JSON mode on ``v1beta``; ``v1`` rejects
    it with ``400 INVALID_ARGUMENT: JSON mode is not enabled for api version
    v1``.  google-genai 3.0.0 flipped the SDK default from ``v1beta`` to ``v1``,
    so the client must pin the version explicitly instead of inheriting it.
    """

    def setUp(self):
        translation._tls.__dict__.pop("client", None)
        self.addCleanup(lambda: translation._tls.__dict__.pop("client", None))

    def test_client_pins_v1beta_regardless_of_sdk_default(self):
        with patch.object(translation, "_api_key", "test-key"), patch.dict(
            "os.environ", {}, clear=False
        ) as env:
            env.pop("GOOGLE_GENAI_API_VERSION", None)
            client = translation._get_client()

        self.assertEqual(client._api_client._http_options.api_version, "v1beta")


if __name__ == "__main__":
    unittest.main()
