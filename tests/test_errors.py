def test_an_unknown_path_has_the_error_form_of_the_spec(client):
    response = client.get("/v1/nothing-here")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_malformed_body_is_a_bad_request_not_a_422(client):
    response = client.post("/v1/auth/login", json={"username": "alice"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "bad_request"
    assert "password" in error["message"] and "deviceName" in error["message"]
