#!/bin/bash

jwt=$(curl -sk "https://localhost:5000/apis/call?username=jon&password=foobarfoobar" | jq -r .jwt)

curl -X PUT -H "x-api-key: $jwt" -F path="Hota Random/2026.11.16 10.48 jons_template_v4/116.GM1" -F file=@README.md -k "https://localhost:5000/apis/call?scope=homm_saves&route=file&machine=test"

curl -X PUT -H "x-api-key: $jwt" -F path="AUTOSAVE.GM1" -F file=@README.md -k "https://localhost:5000/apis/call?scope=homm_saves&route=file&machine=test"

echo "\n\n"

#changes=$(curl -s -H "x-api-key: $jwt" -k "https://localhost:5000/apis/call?scope=homm_saves&route=changes&machine=test2&since=2026-11-16T19:10:28Z")

changes=$(curl -s -H "x-api-key: $jwt" -k "https://localhost:5000/apis/call?scope=homm_saves&route=changes&machine=test2")
echo "$changes"

echo -e "\n\n"

# download every changed file into ./tmp/, keeping the folder structure
# (store .date from $changes as the next "since")
echo "$changes" | jq -r '.data[].path' | while IFS= read -r path; do
  encoded=$(jq -rn --arg p "$path" '$p | @uri')
  echo "downloading $path"
  curl -s -f -k --create-dirs -o "./tmp/$path" -H "x-api-key: $jwt" \
    "https://localhost:5000/apis/call?scope=homm_saves&route=file&machine=test2&path=$encoded" \
    || echo "  failed: $path"
done
