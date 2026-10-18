#!/usr/bin/env bash
# Deploy (or redeploy) the cross-team test environment to one Compute Engine VM.
#
#   deploy/gcp/deploy.sh            # from the repository root, with an authenticated gcloud
#
# Idempotent: creates the static IP, firewall rule and VM only if they don't exist, then uploads
# the committed HEAD (git archive, so uncommitted changes are NOT deployed) and restarts the stack.
# Works from Cloud Shell, Linux, macOS or Git Bash on Windows (set GCLOUD=gcloud.cmd there).
set -euo pipefail

PROJECT="${PROJECT:-aireg-512020}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
VM="${VM:-compliance-monitor}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
GCLOUD="${GCLOUD:-gcloud}"

g() { "$GCLOUD" --project "$PROJECT" --quiet "$@"; }

echo "== project $PROJECT: VM $VM ($MACHINE_TYPE) in $ZONE"
g services enable compute.googleapis.com

if ! g compute addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1; then
    g compute addresses create "$VM-ip" --region "$REGION"
fi
IP="$(g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)')"
SITE_ADDRESS="${SITE_ADDRESS:-${IP//./-}.sslip.io}"

if ! g compute firewall-rules describe "$VM-web" >/dev/null 2>&1; then
    g compute firewall-rules create "$VM-web" --network default \
        --allow tcp:80,tcp:443 --target-tags "$VM-web" \
        --description "HTTPS for the compliance monitor test deployment"
fi

if ! g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
    g compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE_TYPE" \
        --image-family debian-12 --image-project debian-cloud \
        --boot-disk-size 20GB --boot-disk-type pd-standard \
        --address "$IP" --tags "$VM-web" \
        --metadata-from-file startup-script=deploy/gcp/vm-startup.sh
fi

echo "== waiting for Docker on the VM (first boot installs it)"
for attempt in $(seq 1 60); do
    if g compute ssh "$VM" --zone "$ZONE" --command "sudo docker compose version" >/dev/null 2>&1; then
        break
    fi
    [ "$attempt" -eq 60 ] && { echo "Docker did not come up on $VM" >&2; exit 1; }
    sleep 10
done

echo "== uploading $(git rev-parse --short HEAD)"
release="$(mktemp -d)"
trap 'rm -rf "$release"' EXIT
# autocrlf off: the release runs on Linux, so it must keep the repository's LF line endings.
git -c core.autocrlf=false archive --format=tar.gz -o "$release/release.tgz" HEAD
# "$VM:." is the remote home directory for both OpenSSH scp and PuTTY's pscp (Windows), which
# doesn't expand "~/".
g compute scp --zone "$ZONE" "$release/release.tgz" deploy/gcp/remote-up.sh deploy/gcp/issue-keys.sh "$VM:."

g compute ssh "$VM" --zone "$ZONE" --command "SITE_ADDRESS='$SITE_ADDRESS' bash ~/remote-up.sh"
echo "== deployed: https://$SITE_ADDRESS   (health: https://$SITE_ADDRESS/healthz)"
echo "   Issue keys for a team: $GCLOUD compute ssh $VM --zone $ZONE --command 'bash ~/issue-keys.sh <team>'"
