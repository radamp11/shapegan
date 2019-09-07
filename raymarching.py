import torch
import numpy as np
import random
from tqdm import tqdm
from PIL import Image
import os

from model.sdf_net import SDFNet, LATENT_CODES_FILENAME
from util import device
from scipy.spatial.transform import Rotation

BATCH_SIZE = 100000

def get_rotation_matrix(angle, axis='y'):
    rotation = Rotation.from_euler(axis, angle, degrees=True)
    matrix = np.identity(4)
    matrix[:3, :3] = rotation.as_dcm()
    return matrix

def get_camera_transform(camera_distance, rotation_y, rotation_x):
    camera_pose = np.identity(4)
    camera_pose[2, 3] = -camera_distance
    camera_pose = np.matmul(camera_pose, get_rotation_matrix(rotation_x, axis='x'))
    camera_pose = np.matmul(camera_pose, get_rotation_matrix(rotation_y, axis='y'))

    return camera_pose

def get_default_coordinates():
    camera_pose = get_camera_transform(2.2, 147, 20)
    camera_position = np.matmul(np.linalg.inv(camera_pose), np.array([0, 0, 0, 1]))[:3]
    light_matrix = get_camera_transform(6, 164, 50)
    light_position = np.matmul(np.linalg.inv(light_matrix), np.array([0, 0, 0, 1]))[:3]
    return camera_position, light_position

camera_position, light_position = get_default_coordinates()

def get_sdf(sdf_net, points, latent_codes):
    with torch.no_grad():
        batch_count = points.shape[0] // BATCH_SIZE
        result = torch.zeros((points.shape[0]), device=points.device)
        for i in range(batch_count):
            result[BATCH_SIZE * i:BATCH_SIZE * (i+1)] = sdf_net.forward(points[BATCH_SIZE * i:BATCH_SIZE * (i+1), :], latent_codes[:BATCH_SIZE, :])
        remainder = points.shape[0] - BATCH_SIZE * batch_count
        result[BATCH_SIZE * batch_count:] = sdf_net.forward(points[BATCH_SIZE * batch_count:, :], latent_codes[:remainder, :])
    return result

def get_normals(sdf_net, points, latent_code):
    batch_count = points.shape[0] // BATCH_SIZE
    result = torch.zeros((points.shape[0], 3), device=points.device)
    for i in range(batch_count):
        result[BATCH_SIZE * i:BATCH_SIZE * (i+1), :] = sdf_net.get_normals(latent_code, points[BATCH_SIZE * i:BATCH_SIZE * (i+1), :])
    remainder = points.shape[0] - BATCH_SIZE * batch_count
    result[BATCH_SIZE * batch_count:, :] = sdf_net.get_normals(latent_code, points[BATCH_SIZE * batch_count:, :])
    return result


def get_shadows(sdf_net, points, light_position, latent_code, threshold = 0.001, radius=1.0):
    ray_directions = light_position[np.newaxis, :] - points
    ray_directions /= np.linalg.norm(ray_directions, axis=1)[:, np.newaxis]
    ray_directions_t = torch.tensor(ray_directions, device=device, dtype=torch.float32)
    points = torch.tensor(points, device=device, dtype=torch.float32)
    
    points += ray_directions_t * 0.1

    indices = torch.arange(points.shape[0])
    shadows = torch.zeros(points.shape[0], dtype=torch.uint8)

    latent_codes = latent_code.repeat(min(indices.shape[0], BATCH_SIZE), 1)

    for i in tqdm(range(200)):
        test_points = points[indices, :]
        sdf = get_sdf(sdf_net, test_points, latent_codes)
        sdf = torch.clamp_(sdf, -0.1, 0.1)
        points[indices, :] += ray_directions_t[indices, :] * sdf.unsqueeze(1)
        
        hits = (sdf > 0) & (sdf < threshold)
        shadows[indices[hits]] = 1
        indices = indices[~hits]
        
        misses = points[indices, 1] > radius
        indices = indices[~misses]
        
        if indices.shape[0] < 2:
            return

    shadows[indices] = 1
    return shadows.cpu().numpy().astype(bool)
    

def get_image(sdf_net, latent_code, resolution = 800, focal_distance = 1.75, threshold = 0.0005, iterations=1000, ssaa=2, radius=1):
    camera_forward = camera_position / np.linalg.norm(camera_position) * -1
    camera_distance = np.linalg.norm(camera_position).item()
    up = np.array([0, 1, 0])
    camera_right = np.cross(camera_forward, up)
    camera_up = np.cross(camera_forward, camera_right)
    
    screenspace_points = np.meshgrid(
        np.linspace(-1, 1, resolution * ssaa),
        np.linspace(-1, 1, resolution * ssaa),
    )
    screenspace_points = np.stack(screenspace_points)
    screenspace_points = screenspace_points.reshape(2, -1).transpose()
    
    points = np.tile(camera_position, (screenspace_points.shape[0], 1))
    points = points.astype(np.float32)
    
    ray_directions = screenspace_points[:, 0] * camera_right[:, np.newaxis] \
        + screenspace_points[:, 1] * camera_up[:, np.newaxis] \
        + focal_distance * camera_forward[:, np.newaxis]
    ray_directions = ray_directions.transpose().astype(np.float32)
    ray_directions /= np.linalg.norm(ray_directions, axis=1)[:, np.newaxis]

    b = np.einsum('ij,ij->i', points, ray_directions) * 2
    c = np.dot(camera_position, camera_position) - radius * radius
    distance_to_sphere = (-b - np.sqrt(np.power(b, 2) - 4 * c)) / 2
    indices = np.argwhere(np.isfinite(distance_to_sphere)).reshape(-1)

    points[indices] += ray_directions[indices] * distance_to_sphere[indices, np.newaxis]

    points = torch.tensor(points, device=device, dtype=torch.float32)
    ray_directions_t = torch.tensor(ray_directions, device=device, dtype=torch.float32)

    indices = torch.tensor(indices, device=device, dtype=torch.int64)
    model_mask = torch.zeros(points.shape[0], dtype=torch.uint8)

    latent_codes = latent_code.repeat(min(indices.shape[0], BATCH_SIZE), 1)

    for i in tqdm(range(iterations)):
        test_points = points[indices, :]
        sdf = get_sdf(sdf_net, test_points, latent_codes)
        sdf = torch.clamp_(sdf, -0.02, 0.02)
        points[indices, :] += ray_directions_t[indices, :] * sdf.unsqueeze(1)
        
        hits = (sdf > 0) & (sdf < threshold)
        model_mask[indices[hits]] = 1
        indices = indices[~hits]
        
        misses = torch.norm(points[indices, :], dim=1) > radius
        indices = indices[~misses]
        
        if indices.shape[0] < 2:
            break
        
    model_mask[indices] = 1

    normal = get_normals(sdf_net, points[model_mask], latent_code).cpu().numpy()

    model_mask = model_mask.cpu().numpy().astype(bool)
    points = points.cpu().numpy()
    model_points = points[model_mask]
    
    seen_by_light = 1.0 - get_shadows(sdf_net, model_points, light_position, latent_code, radius=radius)
    
    light_direction = light_position[np.newaxis, :] - model_points
    light_direction /= np.linalg.norm(light_direction, axis=1)[:, np.newaxis]
    
    diffuse = np.einsum('ij,ij->i', light_direction, normal)
    diffuse = np.clip(diffuse, 0, 1) * seen_by_light

    reflect = light_direction - np.einsum('ij,ij->i', light_direction, normal)[:, np.newaxis] * normal * 2
    reflect /= np.linalg.norm(reflect, axis=1)[:, np.newaxis]
    specular = np.einsum('ij,ij->i', reflect, ray_directions[model_mask, :])
    specular = np.clip(specular, 0.0, 1.0)
    specular = np.power(specular, 20) * seen_by_light
    rim_light = -np.einsum('ij,ij->i', normal, ray_directions[model_mask, :])
    rim_light = 1.0 - np.clip(rim_light, 0, 1)
    rim_light = np.power(rim_light, 4) * 0.3

    color = np.array([0.8, 0.1, 0.1])[np.newaxis, :] * (diffuse * 0.5 + 0.5)[:, np.newaxis]
    color += (specular * 0.3 + rim_light)[:, np.newaxis]

    color = np.clip(color, 0, 1)

    ground_points = ray_directions[:, 1] < 0
    ground_points[model_mask] = 0
    ground_points = np.argwhere(ground_points).reshape(-1)
    ground_plane = np.min(model_points[:, 1]).item()
    points[ground_points, :] -= ray_directions[ground_points, :] * ((points[ground_points, 1] - ground_plane) / ray_directions[ground_points, 1])[:, np.newaxis]    
    ground_points = ground_points[np.linalg.norm(points[ground_points, ::2], axis=1) < 3]

    ground_shadows = get_shadows(sdf_net, points[ground_points, :], light_position, latent_code)
    
    pixels = np.ones((points.shape[0], 3))
    pixels[model_mask] = color
    pixels[ground_points[ground_shadows]] = 0.4
    pixels = pixels.reshape((resolution * ssaa, resolution * ssaa, 3))

    image = Image.fromarray(np.uint8(pixels * 255) , 'RGB')

    if ssaa != 1:
        image = image.resize((resolution, resolution), Image.ANTIALIAS)

    return image



def get_image_for_index(sdf_net, latent_codes, index):
    FILENAME = 'screenshots/raymarching-examples/image-{:d}.png'
    filename = FILENAME.format(index)

    if os.path.isfile(filename):
        return Image.open(filename)
    
    img = get_image(sdf_net, latent_codes[index])
    img.save(filename)
    return img

if __name__ == "__main__":
    sdf_net = SDFNet()
    sdf_net.load()
    sdf_net.eval()
    latent_codes = torch.load(LATENT_CODES_FILENAME).to(device)

    codes = list(range(latent_codes.shape[0]))
    random.shuffle(codes)

    for i in codes:
        img = get_image_for_index(sdf_net, latent_codes, i)
        img.show()