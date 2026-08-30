import cv2
import numpy as np

def create_gradient_background(width, height, color1, color2):
    """Creates a vertical linear gradient."""
    base = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        ratio = y / height
        color = color1 * (1 - ratio) + color2 * ratio
        base[y, :] = color
    return base

def main():
    width, height = 1024, 1024
    
    # Deep purple to navy gradient
    color1 = np.array([80, 0, 40])  # BGR
    color2 = np.array([20, 0, 10])
    
    img = create_gradient_background(width, height, color1, color2)
    
    # Add some stylized circles (glow effect)
    cv2.circle(img, (width//2, height//2), 400, (120, 30, 60), -1, cv2.LINE_AA)
    cv2.circle(img, (width//2, height//2), 300, (180, 50, 90), -1, cv2.LINE_AA)
    
    # Add a central symbol (e.g., a musical note or a simple geometric shape)
    # Let's do a stylized 'M' or just a play button shape
    pts = np.array([[350, 300], [750, 512], [350, 724]], np.int32)
    cv2.fillPoly(img, [pts], (255, 255, 255), cv2.LINE_AA)
    
    # Apply a slight blur for a soft look
    img = cv2.GaussianBlur(img, (5, 5), 0)
    
    output_path = "app_icon.png"
    cv2.imwrite(output_path, img)
    print(f"Icon generated at {output_path}")

if __name__ == "__main__":
    main()
