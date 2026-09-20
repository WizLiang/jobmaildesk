from job_mail_desk.data_lock import data_directory_lease


def test_data_directory_lease_is_reentrant_in_process(tmp_path) -> None:
    path = tmp_path / ".data.lock"

    with data_directory_lease(path):
        with data_directory_lease(path):
            assert path.exists()
