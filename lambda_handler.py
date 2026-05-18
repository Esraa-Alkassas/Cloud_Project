import json
import boto3
import os

s3 = boto3.client('s3', region_name='us-east-1')
BUCKET_NAME = 'esp32-firmware-rewaa'
MANIFEST_FILE = 'manifest.json'

def lambda_handler(event, context):
    print(f"Full Event: {json.dumps(event)}")
    
    # Handle missing query params safely
    query_params = event.get('queryStringParameters') or {}
    current_version = query_params.get('hash', 'unknown')
    force_full = query_params.get('force_full', '0') == '1'
    
    print(f"Request Version: {current_version}")
    
    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=MANIFEST_FILE)
        manifest = json.loads(response['Body'].read().decode('utf-8'))
        print(f"Manifest loaded: {json.dumps(manifest)}")
    except Exception as e:
        print(f"S3 Error: {e}")
        return {
            'statusCode': 200, 
            'body': json.dumps({'update_available': False, 'error': f'S3 Error: {str(e)}'})
        }

    latest_version = manifest.get('latest_version')
    delta_patches = manifest.get('delta_patches', {})

    if current_version == latest_version:
        print("Device is already at the latest version.")
        return {
            'statusCode': 200,
            'body': json.dumps({'update_available': False})
        }

    is_delta = False
    update_file = f"build_{latest_version}.bin"

    if current_version in delta_patches and not force_full:
        is_delta = True
        update_file = delta_patches[current_version]
        print(f"Found delta patch: {update_file}")
    else:
        print(f"No delta patch for {current_version}. Using full binary: {update_file}")

    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': update_file},
            ExpiresIn=3600
        )
        return {
            'statusCode': 200,
            'body': json.dumps({
                'update_available': True,
                'is_delta': is_delta,
                'download_url': url,
                'latest_version': latest_version
            })
        }
    except Exception as e:
        print(f"Presign Error: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Failed to generate S3 URL'})
        }
