import json
import boto3
import os

s3 = boto3.client('s3')
BUCKET_NAME = 'esp32-firmware-rewaa'
MANIFEST_FILE = 'manifest.json'

def lambda_handler(event, context):
    query_params = event.get('queryStringParameters', {})
    current_version = query_params.get('hash', 'unknown')
    
    print(f"Device version: {current_version}")
    
    try:
        # 1. Fetch manifest.json from S3
        response = s3.get_object(Bucket=BUCKET_NAME, Key=MANIFEST_FILE)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
    except Exception as e:
        print(f"Error reading manifest: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Failed to read manifest'})
        }

    latest_version = manifest.get('latest_version')
    delta_patches = manifest.get('delta_patches', {})

    # If the device is already at the latest version
    if current_version == latest_version:
        return {
            'statusCode': 200,
            'body': json.dumps({'update_available': False})
        }

    # 2. Determine update path: Delta or Full
    is_delta = False
    update_file = f"build_{latest_version}.bin"

    if current_version in delta_patches:
        is_delta = True
        update_file = delta_patches[current_version]
        print(f"Found delta patch: {update_file}")
    else:
        print(f"No delta patch found for {current_version}. Falling back to full binary: {update_file}")

    # 3. Generate Pre-signed URL
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': update_file},
            ExpiresIn=3600
        )
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Failed to generate download URL'})
        }

    return {
        'statusCode': 200,
        'body': json.dumps({
            'update_available': True,
            'is_delta': is_delta,
            'download_url': url,
            'latest_version': latest_version
        })
    }
