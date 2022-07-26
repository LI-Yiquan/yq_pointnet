import torch
import numpy as np
from vanilla_gradient import VanillaGradient


class IntegratedGradients(VanillaGradient):
    def get_mask(self, image_tensor, target_class=None, baseline='black', steps=25, process=lambda x: x):
        if baseline is 'black':
            baseline = torch.ones_like(image_tensor) * torch.min(image_tensor).detach().cpu()
        elif baseline is 'white':
            baseline = torch.ones_like(image_tensor) * torch.max(image_tensor).detach().cpu()
        else:
            baseline = torch.zeros_like(image_tensor)

        batch, channels, place = image_tensor.size()
        print(image_tensor.size())
        grad_sum = np.zeros((place, channels, batch))
        image_diff = image_tensor - baseline

        grad_sum = np.moveaxis(grad_sum, 1, 0)
        for step, alpha in enumerate(np.linspace(0, 1, steps)):
            #print('Processing Integrated Gradients at literation: ', step)
            image_step = baseline + alpha * image_diff
            grad_sum += process(super(IntegratedGradients, self).get_mask(image_step, target_class))
        return grad_sum * np.moveaxis(image_diff.detach().cpu().numpy(), 0, -1) / steps