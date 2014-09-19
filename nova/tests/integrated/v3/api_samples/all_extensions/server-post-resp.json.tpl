{
    "server": {
        "adminPass": "%(password)s",
        "id": "%(id)s",
        "links": [
            {
                "href": "%(host)s/v3/servers/%(uuid)s",
                "rel": "self"
            },
            {
                "href": "%(host)s/servers/%(uuid)s",
                "rel": "bookmark"
            }
        ],
        "os-security-groups:security_groups": [
            {
                "name": "default"
            }
        ],
        "accessIPv4": "",
        "accessIPv6": ""
    }
}
