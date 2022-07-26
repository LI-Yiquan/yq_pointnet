import pdb
import time
import torch
import torch.optim as optim
import torch.nn.functional as F
import numpy as np

def rand_row(array):
    row_total = array.shape[1]
    row_sequence = np.arange(row_total)
    np.random.shuffle(row_sequence)
    return array[:, row_sequence, :]


def get_critical_points(model, pc, label, num):
    """Get top num important point coordinates for given model and pc.
    Args:
        model (torch.nn.Module): model to evaluate
        pc (torch.FloatTensor): input batch pc, [B, 3, K]
        label (torch.LontTensor): batch label, [B]
        num (int): number we want
    """
    B = label.shape[0]
    input_pc = pc.clone().detach().float().cuda()
    input_pc.requires_grad_()
    label = label.long().cuda()
    model.eval()
    # input and calculate gradient
    logits = model(input_pc)
    if isinstance(logits, tuple):  # PointNet
        logits = logits[0]
    loss = F.cross_entropy(logits, label)
    loss.backward()
    with torch.no_grad():
        grad = input_pc.grad.data  # [B, 3, K]
        grad = torch.sum(grad ** 2, dim=1)  # [B, K]
        # get top index of [B, num]
        _, idx = grad.topk(k=num, dim=-1)
        critical_points = torch.stack([
            pc[i, :, idx[i]] for i in range(B)
        ], dim=0).clone().detach()  # [B, 3, num]
    return critical_points


class CWAdd:
    """Class for CW attack.
    """

    def __init__(self, model, trans_model,adv_func, dist_func, attack_lr=1e-2,
                 init_weight=5e3, max_weight=4e4, binary_step=10,
                 num_iter=500, num_add=512, attack_method='untarget'):
        """CW attack by adding points.
        Args:
            model (torch.nn.Module): victim model
            adv_func (function): adversarial loss function
            dist_func (function): distance metric
            attack_lr (float, optional): lr for optimization. Defaults to 1e-2.
            init_weight (float, optional): weight factor init. Defaults to 10.
            max_weight (float, optional): max weight factor. Defaults to 80.
            binary_step (int, optional): binary search step. Defaults to 10.
            num_iter (int, optional): max iter num in every search step. Defaults to 500.
            num_add (int, optional): number of adding points. Default to 512.
        """

        self.model = model.cuda()
        self.model.eval()

        self.trans_model = trans_model
        self,trans_model.eval()

        self.adv_func = adv_func
        self.dist_func = dist_func
        self.attack_lr = attack_lr
        self.init_weight = init_weight
        self.max_weight = max_weight
        self.binary_step = binary_step
        self.num_iter = num_iter
        self.num_add = num_add
        self.attack_method = attack_method
        self.attack_fail = 0
        self.shuffle_fail = 0
        self.trans_fail = 0

    def attack(self, data, target):
        """Attack on given data to target.
        Args:
            data (torch.FloatTensor): victim data, [B, num_points, 3]
            target (torch.LongTensor): target output, [B]
        """
        B, K = data.shape[:2]
        data = data.float().cuda().detach()
        data = data.transpose(1, 2).contiguous()  # [B, 3, K]
        ori_data = data.clone().detach()  # [B, 3, K]
        ori_data.requires_grad = False
        target = target.long().cuda().detach()  # [B]
        label_val = target.detach().cpu().numpy()  # [B]

        # weight factor for budget regularization
        lower_bound = np.zeros((B,))
        upper_bound = np.ones((B,)) * self.max_weight
        current_weight = np.ones((B,)) * self.init_weight

        # record best results in binary search
        o_bestdist = np.array([1e10] * B)
        o_bestscore = np.array([-1] * B)
        o_bestattack = np.zeros((B, 3, self.num_add))
        cri_data = get_critical_points(
            self.model, ori_data, target, self.num_add)

        logits, _, _ = self.model(ori_data)  # [B, num_classes]
        pred = torch.argmax(logits, dim=1)
        print("ori label:", pred.item())
        if self.attack_method == 'top1_error':
            pred_max1 = logits.topk(5, dim=1, largest=True, sorted=True)[1][0][1]
            target =label_val= pred_max1.unsqueeze(0)
            print(target.item())

        # perform binary search
        for binary_step in range(self.binary_step):
            # init with critical points and some small noise!
            adv_data = cri_data + \
                torch.randn((B, 3, self.num_add)).cuda() * 1e-7
            adv_data.requires_grad_()  # [B, 3, num]
            bestdist = np.array([1e10] * B)
            bestscore = np.array([-1] * B)
            opt = optim.Adam([adv_data], lr=self.attack_lr, weight_decay=0.)

            adv_loss = torch.tensor(0.).cuda()
            dist_loss = torch.tensor(0.).cuda()

            total_time = 0.
            forward_time = 0.
            backward_time = 0.
            update_time = 0.

            # one step in binary search
            for iteration in range(self.num_iter):
                t1 = time.time()

                # forward passing
                # concat added points with real pc!
                cat_data = torch.cat([ori_data, adv_data], dim=-1)
                logits = self.model(cat_data)  # [B, num_classes]
                if isinstance(logits, tuple):  # PointNet
                    logits = logits[0]

                t2 = time.time()
                forward_time += t2 - t1

                # print
                pred = torch.argmax(logits, dim=-1)  # [B]

                if self.attack_method == "untarget":
                    success_num = (pred != target).sum().item()
                else:
                    success_num = (pred == target).sum().item()

                # record values
                dist_val = self.dist_func(
                    adv_data.transpose(1, 2).contiguous(),
                    ori_data.transpose(1, 2).contiguous(),
                    batch_avg=False).detach().cpu().numpy()  # [B]
                pred_val = pred.detach().cpu().numpy()  # [B]
                input_val = adv_data.detach().cpu().numpy()  # [B, 3, K]

                # update
                for e, (dist, pred, label, ii) in \
                        enumerate(zip(dist_val, pred_val, label_val, input_val)):
                    if self.attack_method == 'untarget':
                        if dist < bestdist[e] and pred != label:
                            bestdist[e] = dist
                            bestscore[e] = pred
                        if dist < o_bestdist[e] and pred != label:
                            o_bestdist[e] = dist
                            o_bestscore[e] = pred
                            o_bestattack[e] = ii
                    else:
                        if dist < bestdist[e] and pred == label:
                            bestdist[e] = dist
                            bestscore[e] = pred
                        if dist < o_bestdist[e] and pred == label:
                            o_bestdist[e] = dist
                            o_bestscore[e] = pred
                            o_bestattack[e] = ii


                t3 = time.time()
                update_time += t3 - t2

                # compute loss and backward
                adv_loss = self.adv_func(logits, target).mean()
                dist_loss = self.dist_func(
                    adv_data.transpose(1, 2).contiguous(),
                    ori_data.transpose(1, 2).contiguous(),
                    weights=torch.from_numpy(current_weight)).mean()
                loss = adv_loss + dist_loss
                opt.zero_grad()
                loss.backward()
                opt.step()

                t4 = time.time()
                backward_time += t4 - t3
                total_time += t4 - t1



            # adjust weight factor
            for e, label in enumerate(label_val):
                if self.attack_method == 'untarget':
                    if bestscore[e] != label and bestscore[e] != -1 and bestdist[e] <= o_bestdist[e]:
                        # success
                        lower_bound[e] = max(lower_bound[e], current_weight[e])
                        current_weight[e] = (lower_bound[e] + upper_bound[e]) / 2.
                    else:
                        # failure
                        upper_bound[e] = min(upper_bound[e], current_weight[e])
                        current_weight[e] = (lower_bound[e] + upper_bound[e]) / 2.
                else:
                    if bestscore[e] == label and bestscore[e] != -1 and bestdist[e] <= o_bestdist[e]:
                        # success
                        lower_bound[e] = max(lower_bound[e], current_weight[e])
                        current_weight[e] = (lower_bound[e] + upper_bound[e]) / 2.
                    else:
                        # failure
                        upper_bound[e] = min(upper_bound[e], current_weight[e])
                        current_weight[e] = (lower_bound[e] + upper_bound[e]) / 2.

            torch.cuda.empty_cache()

        # end of CW attack
        # fail to attack some examples
        # just assign them with last time attack data
        fail_idx = (lower_bound == 0.)
        o_bestattack[fail_idx] = input_val[fail_idx]  # [B, 3, num]

        # return final results
        success_num = (lower_bound > 0.).sum()
        print('Successfully attack {}/{}'.format(success_num, B))
        # Test attack
        attack_result = o_bestattack
        attack_result = torch.from_numpy(attack_result)
        attack_result = attack_result.float().cuda()
        attack_logits, _, _ = self.model(attack_result)
        print('attack result: ', torch.argmax(attack_logits, dim=1).item())
        if self.attack_method == 'untarget':
            if torch.argmax(attack_logits, dim=1) == target:
                self.attack_fail += 1
                print("attack fail: ", self.attack_fail)
        else:
            if torch.argmax(attack_logits, dim=1) != target:
                self.attack_fail += 1
                print("attack fail: ", self.attack_fail)

        # Test shuffle attack
        attack_result = o_bestattack.transpose((0, 2, 1))
        attack_result = rand_row(attack_result)
        attack_result = torch.from_numpy(attack_result.transpose((0, 2, 1)))
        attack_result = attack_result.float().cuda()

        shuffle_logits, _, _ = self.model(attack_result)
        print('shuffle result: ', torch.argmax(shuffle_logits, dim=1).item())
        if self.attack_method == 'untarget':
            if torch.argmax(shuffle_logits, dim=1) == target:
                self.shuffle_fail += 1
                print("shuffle fail: ", self.shuffle_fail)
        else:
            if torch.argmax(shuffle_logits, dim=1) != target:
                self.shuffle_fail += 1
                print("shuffle fail: ", self.shuffle_fail)

        # Test transfer attack
        transfer_result = o_bestattack
        transfer_result = torch.from_numpy(transfer_result)
        transfer_result = transfer_result.float().cuda()
        transfer_logits, _, _ = self.trans_model(transfer_result)
        print('transfer result: ', torch.argmax(transfer_logits, dim=1).item())
        if self.attack_method == 'untarget':
            if torch.argmax(transfer_logits, dim=1) == target:
                self.trans_fail += 1
                print("trans fail: ", self.trans_fail)
        else:
            if torch.argmax(transfer_logits, dim=1) != target:
                self.trans_fail += 1
                print("trans fail: ", self.trans_fail)


        # concatenate added and clean points
        ori_data = ori_data.detach().cpu().numpy()  # [B, 3, K]
        o_bestattack = np.concatenate([ori_data, o_bestattack], axis=-1)
        return o_bestdist, o_bestattack.transpose((0, 2, 1)), success_num