from cryptography.fernet import Fernet

from job_mail_desk.private_link_store import PrivateLinkStore


def test_private_link_store_persists_only_encrypted_reference(
    tmp_path,
    monkeypatch,
) -> None:
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(PrivateLinkStore, "_cipher", staticmethod(lambda: cipher))
    store = PrivateLinkStore(tmp_path / "private-links.json")
    url = "https://exam.example.invalid/private-token"

    reference = store.put("a" * 32, url)

    assert reference and reference.startswith("pl1_")
    assert store.get(reference) == url
    persisted = store.path.read_text(encoding="utf-8")
    assert url not in persisted
    assert "private-token" not in persisted
