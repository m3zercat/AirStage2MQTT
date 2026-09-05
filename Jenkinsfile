pipeline {
    agent { label 'docker' }

    options {
        buildDiscarder(logRotator(numToKeepStr: '25'))
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
    }

    triggers {
        // Rebuild even without source changes to pick up Python base-image updates.
        cron('H H * * *')
    }

    environment {
        RELEASE_LINE = '0.1'
        IMAGE_REPOSITORY = 'airstage2mqtt'
        CI_NETWORK = "airstage2mqtt-ci-${BUILD_NUMBER}"
        CI_MQTT = "airstage2mqtt-ci-mqtt-${BUILD_NUMBER}"
        CI_AIRSTAGE = "airstage2mqtt-ci-unit-${BUILD_NUMBER}"
        CI_APP = "airstage2mqtt-ci-app-${BUILD_NUMBER}"
        CI_DATA_VOLUME = "airstage2mqtt-ci-data-${BUILD_NUMBER}"
    }

    stages {
        stage('Validate and create metadata') {
            steps {
                sh '''
                    set -eu
                    : "${CONTAINER_REGISTRY_READ:?Configure CONTAINER_REGISTRY_READ in Jenkins}"
                    : "${CONTAINER_REGISTRY_PUSH:?Configure CONTAINER_REGISTRY_PUSH in Jenkins}"
                    : "${CI_GIT_USER_NAME:?Configure CI_GIT_USER_NAME in Jenkins}"
                    : "${CI_GIT_USER_EMAIL:?Configure CI_GIT_USER_EMAIL in Jenkins}"
                    : "${GIT_PUSH_CREDENTIALS_ID:?Configure GIT_PUSH_CREDENTIALS_ID in Jenkins}"
                    case "$CONTAINER_REGISTRY_READ$CONTAINER_REGISTRY_PUSH" in
                        *://*) echo 'Registry values must be host[:port], without a URL scheme.' >&2; exit 1 ;;
                    esac
                '''
                script {
                    env.SOURCE_REVISION = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
                    env.SOURCE_REVISION_SHORT = sh(
                        returnStdout: true,
                        script: 'git rev-parse --short=12 HEAD'
                    ).trim()
                    env.SOURCE_URL = sh(
                        returnStdout: true,
                        script: 'git config --get remote.origin.url'
                    ).trim()
                    env.VERSION = "v${env.RELEASE_LINE}.${env.BUILD_NUMBER}"
                    env.SHA_TAG = "sha-${env.SOURCE_REVISION_SHORT}"
                    env.RUN_TAG = "run-${env.SOURCE_REVISION_SHORT}-${env.BUILD_NUMBER}"
                    env.CREATED = sh(
                        returnStdout: true,
                        script: 'date -u +%Y-%m-%dT%H:%M:%SZ'
                    ).trim()
                    env.TEST_IMAGE = "airstage2mqtt-test:${env.RUN_TAG}"
                    env.CANDIDATE_IMAGE = "${env.CONTAINER_REGISTRY_READ}/${env.IMAGE_REPOSITORY}:${env.RUN_TAG}"
                    env.RELEASE_IMAGE = "${env.CONTAINER_REGISTRY_READ}/${env.IMAGE_REPOSITORY}:${env.VERSION}"

                    currentBuild.displayName = "#${env.BUILD_NUMBER} ${env.SOURCE_REVISION_SHORT}"
                    currentBuild.description = "Candidate ${env.RUN_TAG} for ${env.VERSION}"
                }
                sh '''
                    set -eu
                    mkdir -p artifacts
                    : > artifacts/release-images.txt
                    if docker buildx imagetools inspect "$RELEASE_IMAGE" >/dev/null 2>&1; then
                        echo "Refusing to replace existing immutable image tag $RELEASE_IMAGE." >&2
                        exit 1
                    fi
                    if git show-ref --verify --quiet "refs/tags/$VERSION" || \
                        git ls-remote --exit-code --tags origin "refs/tags/$VERSION" >/dev/null 2>&1; then
                        echo "Refusing to replace existing immutable Git tag $VERSION." >&2
                        exit 1
                    fi
                '''
            }
        }

        stage('Quality and unit tests') {
            steps {
                sh '''
                    set -eu
                    docker build --pull --no-cache \
                        --target test \
                        --tag "$TEST_IMAGE" .
                '''
            }
        }

        stage('Build candidate') {
            steps {
                sh '''
                    set -eu
                    docker build --pull --no-cache \
                        --target runtime \
                        --build-arg "A2M_VERSION=$VERSION" \
                        --label "org.opencontainers.image.created=$CREATED" \
                        --label "org.opencontainers.image.revision=$SOURCE_REVISION" \
                        --label "org.opencontainers.image.source=$SOURCE_URL" \
                        --label "org.opencontainers.image.version=$VERSION" \
                        --tag "$CANDIDATE_IMAGE" .
                '''
            }
        }

        stage('Container behaviour tests') {
            steps {
                sh '''
                    set -eu
                    docker network create "$CI_NETWORK"
                    docker volume create "$CI_DATA_VOLUME"

                    docker run -d \
                        --name "$CI_MQTT" \
                        --network "$CI_NETWORK" \
                        --network-alias a2m-ci-mqtt \
                        -v "$PWD/tests/fixtures/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
                        eclipse-mosquitto:2

                    docker run -d \
                        --name "$CI_AIRSTAGE" \
                        --network "$CI_NETWORK" \
                        -e MOCK_DEVICE_ID=E8FB1C000000 \
                        -v "$PWD/tests/fixtures/mock_airstage.py:/fixture/mock_airstage.py:ro" \
                        python:3.14-slim \
                        python /fixture/mock_airstage.py

                    mqtt_ready=false
                    for attempt in $(seq 1 30); do
                        if docker run --rm --network "$CI_NETWORK" \
                            --entrypoint mosquitto_pub eclipse-mosquitto:2 \
                            -h a2m-ci-mqtt -t ci/readiness -m ready >/dev/null 2>&1; then
                            mqtt_ready=true
                            break
                        fi
                        sleep 1
                    done
                    if [ "$mqtt_ready" != true ]; then
                        echo 'The MQTT fixture did not become reachable.' >&2
                        docker logs "$CI_MQTT" || true
                        exit 1
                    fi

                    unit_ip=$(docker inspect \
                        --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
                        "$CI_AIRSTAGE")
                    test -n "$unit_ip"
                    backend_ready=false
                    for attempt in $(seq 1 30); do
                        if docker run --rm --network "$CI_NETWORK" python:3.14-slim \
                            python -c "import urllib.request; urllib.request.urlopen('http://$unit_ip/state', timeout=1)" \
                            >/dev/null 2>&1; then
                            backend_ready=true
                            break
                        fi
                        sleep 1
                    done
                    if [ "$backend_ready" != true ]; then
                        echo 'The AirStage fixture did not become reachable.' >&2
                        docker logs "$CI_AIRSTAGE" || true
                        exit 1
                    fi

                    {
                        echo 'mqtt:'
                        echo '  base_topic: airstage2mqtt'
                        echo 'polling:'
                        echo '  interval_seconds: 2'
                        echo '  timeout_seconds: 1'
                        echo '  retries: 1'
                        echo '  offline_after_failures: 1'
                        echo '  reconnect_min_seconds: 1'
                        echo '  reconnect_max_seconds: 2'
                        echo 'homeassistant:'
                        echo '  enabled: true'
                        echo '  discovery_prefix: homeassistant'
                        echo 'units:'
                        echo '  - name: test_unit'
                        echo '    friendly_name: Test Air Conditioner'
                        echo '    mac: E8FB1C000000'
                        echo "    ip: $unit_ip"
                        echo '    use_https: false'
                        echo '    turn_on_before_set_temperature: true'
                        echo '  - name: offline_unit'
                        echo '    mac: E8FB1C000001'
                        echo '    ip: 192.0.2.1'
                        echo '    use_https: false'
                    } > artifacts/ci-config.yaml

                    docker run -d \
                        --name "$CI_APP" \
                        --network "$CI_NETWORK" \
                        --read-only \
                        --tmpfs /tmp:rw,noexec,nosuid,size=8m \
                        --cap-drop ALL \
                        --security-opt no-new-privileges:true \
                        -e A2M_CONFIG=/config/config.yaml \
                        -e A2M_MQTT_HOST=a2m-ci-mqtt \
                        -e A2M_MQTT_PORT=1883 \
                        -e A2M_LOG_LEVEL=DEBUG \
                        -v "$PWD/artifacts/ci-config.yaml:/config/config.yaml:ro" \
                        -v "$CI_DATA_VOLUME:/data" \
                        "$CANDIDATE_IMAGE"

                    read_retained() {
                        topic=$1
                        docker run --rm --network "$CI_NETWORK" \
                            --entrypoint mosquitto_sub eclipse-mosquitto:2 \
                            -h a2m-ci-mqtt -t "$topic" -C 1 -W 2 2>/dev/null || true
                    }

                    initial_state=''
                    for attempt in $(seq 1 30); do
                        initial_state=$(read_retained airstage2mqtt/test_unit)
                        if printf '%s' "$initial_state" | grep -Fq '"mode":"cool"'; then
                            break
                        fi
                        sleep 1
                    done
                    printf '%s\n' "$initial_state" | grep -F '"state":"ON"'
                    printf '%s\n' "$initial_state" | grep -F '"current_temperature":21.5'
                    test "$(read_retained airstage2mqtt/test_unit/availability)" = online
                    test "$(read_retained airstage2mqtt/offline_unit/availability)" = offline

                    discovery=$(read_retained homeassistant/device/airstage2mqtt_e8fb1c000000/config)
                    printf '%s\n' "$discovery" | grep -F '"platform":"climate"'
                    printf '%s\n' "$discovery" | grep -F '"platform":"switch"'

                    docker run --rm --network "$CI_NETWORK" \
                        --entrypoint mosquitto_pub eclipse-mosquitto:2 \
                        -h a2m-ci-mqtt -t airstage2mqtt/test_unit/set \
                        -m '{"mode":"heat","target_temperature":21.5,"economy":"ON"}'

                    command_state=''
                    for attempt in $(seq 1 30); do
                        command_state=$(read_retained airstage2mqtt/test_unit)
                        if printf '%s' "$command_state" | grep -Fq '"mode":"heat"' && \
                           printf '%s' "$command_state" | grep -Fq '"economy":"ON"'; then
                            break
                        fi
                        sleep 1
                    done
                    printf '%s\n' "$command_state" | grep -F '"target_temperature":21.5'

                    docker run --rm --network "$CI_NETWORK" \
                        --entrypoint mosquitto_pub eclipse-mosquitto:2 \
                        -h a2m-ci-mqtt -t airstage2mqtt/test_unit/get -n
                    docker exec "$CI_APP" python -m airstage2mqtt healthcheck

                    docker stop "$CI_MQTT" >/dev/null
                    docker start "$CI_MQTT" >/dev/null
                    reconnected=false
                    for attempt in $(seq 1 30); do
                        if [ "$(read_retained airstage2mqtt/bridge/state)" = online ]; then
                            reconnected=true
                            break
                        fi
                        sleep 1
                    done
                    test "$reconnected" = true

                    docker kill "$CI_APP" >/dev/null
                    lwt=''
                    for attempt in $(seq 1 15); do
                        lwt=$(read_retained airstage2mqtt/bridge/state)
                        [ "$lwt" = offline ] && break
                        sleep 1
                    done
                    test "$lwt" = offline
                '''
            }
        }

        stage('Publish immutable version image') {
            steps {
                sh '''
                    set -eu
                    release_images=artifacts/release-images.txt
                    read_image="$CONTAINER_REGISTRY_READ/$IMAGE_REPOSITORY:$VERSION"
                    push_image="$CONTAINER_REGISTRY_PUSH/$IMAGE_REPOSITORY:$VERSION"

                    docker image tag "$CANDIDATE_IMAGE" "$push_image"
                    docker push "$push_image"

                    digest=''
                    for attempt in $(seq 1 30); do
                        digest=$(docker buildx imagetools inspect "$read_image" 2>/dev/null | \
                            awk '$1 == "Digest:" { print $2; exit }')
                        [ -n "$digest" ] && break
                        sleep 1
                    done
                    if [ -z "$digest" ]; then
                        echo "Could not determine the registry digest for $read_image." >&2
                        exit 1
                    fi
                    printf '%s@%s\n' "$CONTAINER_REGISTRY_READ/$IMAGE_REPOSITORY" "$digest" \
                        > "$release_images"
                    cat "$release_images"
                '''
            }
        }

        stage('Promote SHA and latest') {
            steps {
                sh '''
                    set -eu
                    source_image="$CONTAINER_REGISTRY_READ/$IMAGE_REPOSITORY:$VERSION"
                    sha_push="$CONTAINER_REGISTRY_PUSH/$IMAGE_REPOSITORY:$SHA_TAG"
                    latest_push="$CONTAINER_REGISTRY_PUSH/$IMAGE_REPOSITORY:latest"
                    sha_read="$CONTAINER_REGISTRY_READ/$IMAGE_REPOSITORY:$SHA_TAG"
                    latest_read="$CONTAINER_REGISTRY_READ/$IMAGE_REPOSITORY:latest"

                    docker buildx imagetools inspect "$source_image" >/dev/null
                    docker buildx imagetools create --tag "$sha_push" "$source_image"
                    docker buildx imagetools create --tag "$latest_push" "$source_image"
                    docker buildx imagetools inspect "$sha_read" >/dev/null
                    docker buildx imagetools inspect "$latest_read" >/dev/null
                '''
            }
        }

        stage('Tag source release') {
            steps {
                sh '''
                    set -eu
                    git -c user.name="$CI_GIT_USER_NAME" \
                        -c user.email="$CI_GIT_USER_EMAIL" \
                        tag --annotate "$VERSION" \
                        --message "Release $VERSION (Jenkins build $BUILD_NUMBER)" \
                        "$SOURCE_REVISION"
                '''
                script {
                    withCredentials(bindings: [sshUserPrivateKey(
                        credentialsId: env.GIT_PUSH_CREDENTIALS_ID,
                        keyFileVariable: 'GIT_SSH_KEY',
                        passphraseVariable: 'GIT_SSH_PASSPHRASE'
                    )]) {
                        sh '''
                            set +x
                            askpass_script=$(mktemp)
                            trap 'rm -f "$askpass_script"' EXIT
                            printf '%s\n' '#!/bin/sh' \
                                'printf "%s\\n" "$GIT_SSH_PASSPHRASE"' > "$askpass_script"
                            chmod 700 "$askpass_script"

                            DISPLAY=jenkins \
                            SSH_ASKPASS="$askpass_script" \
                            SSH_ASKPASS_REQUIRE=force \
                            GIT_SSH_COMMAND='ssh -i "$GIT_SSH_KEY" -o IdentitiesOnly=yes' \
                                git -c url."git@github.com:".insteadOf="https://github.com/" \
                                push origin \
                                "refs/tags/$VERSION:refs/tags/$VERSION"
                        '''
                    }
                }
                sh 'git show --no-patch --format=fuller "$VERSION"'
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                allowEmptyArchive: true,
                artifacts: 'artifacts/release-images.txt'
            )
            sh '''
                docker rm --force "$CI_APP" "$CI_AIRSTAGE" "$CI_MQTT" >/dev/null 2>&1 || true
                docker network rm "$CI_NETWORK" >/dev/null 2>&1 || true
                docker volume rm "$CI_DATA_VOLUME" >/dev/null 2>&1 || true
                if [ -n "${TEST_IMAGE:-}" ]; then
                    docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true
                fi
                if [ -n "${CANDIDATE_IMAGE:-}" ]; then
                    docker image rm "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true
                fi
                if [ -n "${VERSION:-}" ]; then
                    docker image rm \
                        "$CONTAINER_REGISTRY_PUSH/$IMAGE_REPOSITORY:$VERSION" \
                        >/dev/null 2>&1 || true
                fi
            '''
        }
    }
}
