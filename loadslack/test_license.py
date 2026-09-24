"""Licence tests: Ed25519 against RFC 8032 vectors, then the gate behaviour."""
import json
import os
import time
import unittest

from loadslack import _ed25519 as ed
from loadslack.profiles import KEY_PREFIX, License, PAID, FREE, _b64e


def _key(secret, **payload):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{KEY_PREFIX}.{_b64e(body)}.{_b64e(ed.sign(secret, body))}"


class TestEd25519RFC8032(unittest.TestCase):
    def test_vector_1_empty_message(self):
        sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pk = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(ed.public_key(sk), pk)
        self.assertEqual(ed.sign(sk, b""), sig)
        self.assertTrue(ed.verify(pk, b"", sig))

    def test_vector_2_one_byte(self):
        sk = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        pk = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
        sig = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertEqual(ed.public_key(sk), pk)
        self.assertEqual(ed.sign(sk, bytes([0x72])), sig)
        self.assertTrue(ed.verify(pk, bytes([0x72]), sig))

    def test_rejects_tampering(self):
        sk = os.urandom(32)
        pk = ed.public_key(sk)
        sig = ed.sign(sk, b"hello")
        self.assertTrue(ed.verify(pk, b"hello", sig))
        self.assertFalse(ed.verify(pk, b"hellO", sig))
        self.assertFalse(ed.verify(pk, b"hello", sig[:-1] + bytes([sig[-1] ^ 1])))
        self.assertFalse(ed.verify(ed.public_key(os.urandom(32)), b"hello", sig))
        self.assertFalse(ed.verify(pk, b"hello", b"short"))


class TestLicense(unittest.TestCase):
    def setUp(self):
        self.sk = os.urandom(32)
        self.pub = ed.public_key(self.sk).hex()

    def test_valid_key_is_paid_and_gates_features(self):
        lic = License.from_key(_key(self.sk, org="Acme", exp=0, mw=2.5), self.pub)
        self.assertEqual(lic.tier, PAID)
        self.assertTrue(lic.active)
        self.assertEqual(lic.org, "Acme")
        self.assertTrue(lic.gate("grid_sidechain"))
        self.assertFalse(lic.gate("rot_elimination"))   # free features are not gated

    def test_expired_key_degrades_not_breaks(self):
        lic = License.from_key(_key(self.sk, org="A", exp=time.time() - 10, mw=0), self.pub)
        self.assertFalse(lic.active)
        self.assertFalse(lic.gate("grid_sidechain"))

    def test_tampered_payload_rejected(self):
        good = _key(self.sk, org="Acme", exp=1, mw=0)
        prefix, _, sig = good.split(".")
        forged = f"{prefix}.{_b64e(json.dumps({'org': 'Acme', 'exp': 0, 'mw': 0}).encode())}.{sig}"
        self.assertEqual(License.from_key(forged, self.pub).tier, FREE)

    def test_wrong_signer_rejected(self):
        other = os.urandom(32)
        self.assertEqual(License.from_key(_key(other, org="X", exp=0, mw=0), self.pub).tier, FREE)

    def test_junk_and_empty_rejected(self):
        for junk in ("", "invalid_junk_key", "GDPRO.a.b", "GDPRO..", "GDK-LIC-valid.abc", None):
            self.assertEqual(License.from_key(junk, self.pub).tier, FREE)

    def test_unconfigured_public_key_fails_closed(self):
        self.assertEqual(License.from_key(_key(self.sk, org="A", exp=0, mw=0), "").tier, FREE)

    def test_env_cannot_extend_expiry(self):
        os.environ["LOADSLACK_LICENSE_EXPIRES"] = "99999999999"
        try:
            lic = License.from_key(_key(self.sk, org="A", exp=time.time() - 10, mw=0), self.pub)
            self.assertFalse(lic.active)
        finally:
            del os.environ["LOADSLACK_LICENSE_EXPIRES"]

    def test_direct_construction_still_works_for_callers_and_tests(self):
        lic = License(tier=PAID, key="k", expires_at=0)
        self.assertTrue(lic.active and lic.gate("grid_sidechain"))
        self.assertFalse(License(tier=PAID, key="k", expires_at=1.0).active)


if __name__ == "__main__":
    unittest.main()
