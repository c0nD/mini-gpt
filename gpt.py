import torch
import torch.nn as nn
from torch.nn import functional as F

# Hyper-parameters
BATCH_SIZE = 32  # the amt of independent sequences to be processed in parallel
BLOCK_SIZE = 8  # maximum context len for predictions
MAX_ITER = 5500
EVAL_INTERVAL = 500
LEARNING_RATE = 1e-3
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
EVAL_ITERS = 200
N_EMBEDDING_DIM = 32

# -------------------


torch.manual_seed(420_69_1337)

with open('data.txt', 'r') as f:
    text = f.read()
    
# Create vocab
chars = sorted(list(set(text)))
vocab_size = len(chars)

# Create char to int and int to char maps
stoi = {c: i for i, c in enumerate(chars)}
itos = {i: c for i, c in enumerate(chars)}
encode = lambda s: [stoi[c] for c in s]  # encoding a string to a list of ints
decode = lambda x: ''.join([itos[i] for i in x])  # decoding a list of ints to a string


# Training & test splits
data = torch.tensor(encode(text), dtype=torch.long)
n = int(len(data) * 0.8)
train_data, val_data = data[:n], data[n:]

# -------------------


# Loading Data
def get_batch(split):
    # Gen small batch data of x and y
    data = train_data if split == 'train' else val_data
    xs = torch.randint(len(data) - BLOCK_SIZE, (BATCH_SIZE,))
    x = torch.stack([data[i:i+BLOCK_SIZE] for i in xs])
    y = torch.stack([data[i+1:i+BLOCK_SIZE+1] for i in xs])
    x, y = x.to(DEVICE), y.to(DEVICE)
    return x, y


@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(EVAL_ITERS)
        for i in range(EVAL_ITERS):
            x, y = get_batch(split)
            logits, loss = model(x, y)
            losses[i] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

# -------------------

# one head of self-attention
class Head(nn.Module):
    
    def __init__(self, head_size):
        super().__init__()
        # Linear projection to apply to nodes
        self.key = nn.Linear(N_EMBEDDING_DIM, head_size, bias=False)
        self.value = nn.Linear(N_EMBEDDING_DIM, head_size, bias=False)
        self.query = nn.Linear(N_EMBEDDING_DIM, head_size, bias=False)
        self.register_buffer('tril', torch.tril(torch.ones(BLOCK_SIZE, BLOCK_SIZE)))  # buffer is a tensor that is not a parameter
        
    def forward(self, x):
        B, T, C = x.shape
        k = self.key(x)  # (B,T,C)
        q = self.query(x)  # (B,T,C)
        
        # computing affenities
        wei = q @ k.transpose(-2, -1) * (C**-.5)  # (B,T,C ) @ (B,C,T) = (B,T,T)
        wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf'))  # (B, T,T)
        wei = F.softmax(wei, dim=-1)  # (B,T,T)
        
        # weighted aggregation of vals
        v = self.value(x)  # (B,T,C)
        out = wei @ v  # (B,T,T) @ (B,T,C) = (B,T,C)
        return out
    
    
# multiple heads of self-attention in parallel
# concat multiple heads into a single vector of size n_heads * head_size
class MultiHeadAttention(nn.Module):
    
    def __init__(self, n_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(n_heads)])    
        
        
    def forward(self, n):
        # n is (B,T,C)
        return torch.cat([h(n) for h in self.heads], dim=-1)

    
# -------------------

class FeedForward(nn.Module):
    
    def __init__(self, N_EMBEDDING_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(N_EMBEDDING_DIM, N_EMBEDDING_DIM),
            nn.ReLU(),
        )
        
    def forward(self, x):
        return self.net(x)
    
    
# Bigram language model
class Bigram(nn.Module):
    
    def __init__(self):
        super().__init__()
        # each tkn reads the logits for the next tkn from a lookup tbl
        self.token_emb = nn.Embedding(vocab_size, N_EMBEDDING_DIM)
        # also take their position into account
        self.position_emb = nn.Embedding(BLOCK_SIZE, N_EMBEDDING_DIM)
        self.sa_head = MultiHeadAttention(4, N_EMBEDDING_DIM//4)  # 4 communication channels of size 8 (8dim self-attention with 4 heads)
        self.lm_head = nn.Linear(N_EMBEDDING_DIM, vocab_size)
        self.frwd = FeedForward(N_EMBEDDING_DIM)

        
    def forward(self, idx, targets=None):
        B,T = idx.shape
        
        # idx & targets are both (B,T) tensors of ints
        pos_emb = self.position_emb(torch.arange(T, device=DEVICE))  # (T,C) -- positional embeddings
        tkn_embeddings = self.token_emb(idx) # (B,T,C)
        n = pos_emb + tkn_embeddings
        x = self.sa_head(n)  # apply single head (B,T,C)
        x = self.frwd(x)  # (B,T,C)
        logits = self.lm_head(x)  # (B,T,vcb_sz)      
        
        if targets is None:
            loss = None
        else:
            # flatten logits and targets to (B*T, C)
            B, T, C = logits.shape
            logits = logits.view(B*T, C)
            targets = targets.view(B*T)
            loss = F.cross_entropy(logits, targets)
            
        return logits, loss
    
    
    def generate(self, idx, max_new_tkns):
        for i in range(max_new_tkns):
            idx_conditional = idx[:, -BLOCK_SIZE:]  # can never have more than BLOCK_SIZE tokens (dont want to run out of range)
            logits, loss, = self(idx_conditional)  # gets the predictions
            logits = logits[:, -1, :]  # (B,C)
            probs = F.softmax(logits, dim=-1)  # (B,C)
            next_idx = torch.multinomial(probs, num_samples=1)  # (B,1)
            idx = torch.cat([idx, next_idx], dim=-1)  # (B,T+1)
        return idx
    
model = Bigram()
m = model.to(DEVICE)


# pytorch optimization
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

for i_ in range(MAX_ITER):
    
    # evaluate loss on train and val sets
    if i_ % EVAL_INTERVAL == 0:
        losses = estimate_loss()
        print(f'Iteration {i_} | Train Loss: {losses["train"]:.5f} | Val Loss: {losses["val"]:.5f}')
        
    # get batch
    x, y = get_batch('train')
    
    # eval loss
    logits, loss = model(x, y)
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    optimizer.step()
    
# -------------------

# Generate from the model
ctx = torch.zeros((1,1), dtype=torch.long, device=DEVICE)
print(decode(m.generate(ctx, max_new_tkns=500)[0].tolist()))