variable "REGISTRY" {
  default = "ghcr.io/getsoren"
}

variable "VERSION" {
  default = "dev"
}

group "default" {
  targets = [
    "pg15", "pg16", "pg17", "pg18",
    "pg15-network", "pg16-network", "pg17-network", "pg18-network",
    "pg15-anonymize", "pg16-anonymize", "pg17-anonymize", "pg18-anonymize",
  ]
}

target "_common" {
  dockerfile = "docker/Dockerfile"
  context    = "."
  platforms  = ["linux/amd64", "linux/arm64"]
}

target "pg15" {
  inherits = ["_common"]
  args     = { PG_VERSION = "15" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-15", "${REGISTRY}/padmy:latest-15"]
}

target "pg16" {
  inherits = ["_common"]
  args     = { PG_VERSION = "16" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-16", "${REGISTRY}/padmy:latest-16"]
}

target "pg17" {
  inherits = ["_common"]
  args     = { PG_VERSION = "17" }
  tags = [
    "${REGISTRY}/padmy:${VERSION}-17",
    "${REGISTRY}/padmy:latest-17",
    "${REGISTRY}/padmy:${VERSION}",
    "${REGISTRY}/padmy:latest",
  ]
}

target "pg18" {
  inherits = ["_common"]
  args     = { PG_VERSION = "18" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-18", "${REGISTRY}/padmy:latest-18"]
}

target "pg15-network" {
  inherits = ["_common"]
  args     = { PG_VERSION = "15", UV_PARAMS = "--group network" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-network-15", "${REGISTRY}/padmy:latest-network-15"]
}

target "pg16-network" {
  inherits = ["_common"]
  args     = { PG_VERSION = "16", UV_PARAMS = "--group network" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-network-16", "${REGISTRY}/padmy:latest-network-16"]
}

target "pg17-network" {
  inherits = ["_common"]
  args     = { PG_VERSION = "17", UV_PARAMS = "--group network" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-network-17", "${REGISTRY}/padmy:latest-network-17"]
}

target "pg18-network" {
  inherits = ["_common"]
  args     = { PG_VERSION = "18", UV_PARAMS = "--group network" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-network-18", "${REGISTRY}/padmy:latest-network-18"]
}

target "pg15-anonymize" {
  inherits = ["_common"]
  args     = { PG_VERSION = "15", UV_PARAMS = "--extra anonymize" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-anonymize-15", "${REGISTRY}/padmy:latest-anonymize-15"]
}

target "pg16-anonymize" {
  inherits = ["_common"]
  args     = { PG_VERSION = "16", UV_PARAMS = "--extra anonymize" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-anonymize-16", "${REGISTRY}/padmy:latest-anonymize-16"]
}

target "pg17-anonymize" {
  inherits = ["_common"]
  args     = { PG_VERSION = "17", UV_PARAMS = "--extra anonymize" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-anonymize-17", "${REGISTRY}/padmy:latest-anonymize-17"]
}

target "pg18-anonymize" {
  inherits = ["_common"]
  args     = { PG_VERSION = "18", UV_PARAMS = "--extra anonymize" }
  tags     = ["${REGISTRY}/padmy:${VERSION}-anonymize-18", "${REGISTRY}/padmy:latest-anonymize-18"]
}
