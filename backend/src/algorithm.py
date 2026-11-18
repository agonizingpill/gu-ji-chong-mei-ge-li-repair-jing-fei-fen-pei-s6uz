import hashlib,json
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score,confusion_matrix
LEVELS=['low','medium','high']
# 复测仪器口径：含水率、霉面积占比各在正负两个百分点内波动；虫孔为计数观测不参与波动。
SWING=2.0
GRID=21
_model=None
def _tree():
    # 决策树与合成样本完全固定（seed=31），整进程训练一次后复用，概率网格只做预测。
    global _model
    if _model is None:
        rng=np.random.default_rng(31)
        x=np.column_stack([rng.uniform(30,90,600),rng.integers(0,31,600),rng.uniform(0,25,600)])
        y=np.where((x[:,0]>=72)|(x[:,2]>=10),'high',np.where((x[:,0]>=60)|(x[:,1]>=10)|(x[:,2]>=2),'medium','low'))
        train,_=train_test_split(np.arange(len(x)),test_size=.25,random_state=31,stratify=y)
        _model=DecisionTreeClassifier(max_depth=5,min_samples_leaf=4,random_state=31).fit(x[train],y[train]),x,y,train
    return _model
def classify(humidity,holes,mold):
    model,x,y,train=_tree()
    test=np.setdiff1d(np.arange(len(x)),train)
    prediction=model.predict(x[test])
    return str(model.predict([[humidity,holes,mold]])[0]),{'macro_f1':float(f1_score(y[test],prediction,average='macro')),'confusion_matrix':confusion_matrix(y[test],prediction,labels=LEVELS).tolist(),'labels':LEVELS,'train_count':len(train),'test_count':len(test),'seed':31,'dataset_sha256':hashlib.sha256(x.tobytes()+y.tobytes()).hexdigest(),'data_source':'专家规则合成样本；留出指标仅证明内部可复现性'}
def cross_grade_probability(humidity,holes,mold):
    # 在 [含水-2,+2]×[霉-2,+2] 等距网格上枚举（边界裁剪到仪器合法量程），
    # 网格点等权代表测量波动；预测等级与中心点不一致的占比即跨级概率。
    model,_,_,_=_tree()
    base=str(model.predict([[humidity,holes,mold]])[0])
    h=np.linspace(max(0,humidity-SWING),min(100,humidity+SWING),GRID)
    m=np.linspace(max(0,mold-SWING),min(100,mold+SWING),GRID)
    hh,mm=np.meshgrid(h,m,indexing='ij')
    samples=np.column_stack([hh.ravel(),np.full(hh.size,holes),mm.ravel()])
    preds=model.predict(samples)
    total=preds.size
    counts={level:float(np.mean(preds==level)) for level in LEVELS}
    cross=float(np.mean(preds!=base))
    return {'base':base,'cross_grade_probability':round(cross,4),'level_probabilities':{k:round(v,4) for k,v in counts.items()},'grid_points':total,'swing':SWING,'inputs':{'humidity':humidity,'holes':holes,'mold':mold}}
