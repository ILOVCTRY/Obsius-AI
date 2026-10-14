/*
*  图片裁剪
*  imgdom：图片dom对象 也可以传this
*  cutImgType(裁剪当时): 居中裁剪autocut  按比例缩放Scaling  拉伸stretching
*  cutImgParent: 父类选择器 例如div标签的id是mydiv  那么这里就填 #mydiv
*
*  例如：
*  cutImg(this,"autocut","#mydiv")  //传父标签
*  cutImg(this,"autocut")  //不传父标签
*/
function cutImg(imgdom,CutImgType,cutImgParent){
    $(imgdom).each(function(){
        var $this=$(imgdom);

	    $this.css("cssText","");

        var objHeight,objWidth;
        //IE7+浏览器都能兼容的函数封装:获取图片的原始尺寸
        if($this[0].naturalWidth && $this[0].naturalHeight) {
            objWidth = $this[0].naturalWidth;
            objHeight = $this[0].naturalHeight;
        } else {
            var img = new Image();
            img.src = $this[0].src;
            objWidth = img.width;
            objHeight = img.height;
        }

        var parentHeight,parentWidth;
        if (cutImgParent!=undefined){
            parentHeight=$this.parents(cutImgParent).eq(0).height();//图片父容器高度
            parentWidth=$this.parents(cutImgParent).eq(0).width();//图片父容器宽度
        }else{
            if($this.parent().prop('tagName')=='a' || $this.parent().prop('tagName')=='A'){
                parentHeight=$this.parent().parent().height();//图片父容器高度
                parentWidth=$this.parent().parent().width();//图片父容器宽度
            }else{
                parentHeight=$this.parent().height();//图片父容器高度
                parentWidth=$this.parent().width();//图片父容器宽度
            }
        }

        var imgCssText = "";

		if(CutImgType == "autocut"){
			var ratio=objHeight/objWidth;
			if(objHeight > objWidth) {//赋值宽高
                if(parentWidth*ratio < parentHeight){
                    imgCssText = "width:"+parentHeight/ratio+"px!important;height:"+parentHeight+"px!important;";
                }else{
                    imgCssText = "width:"+parentWidth+"px!important;height:"+parentWidth*ratio+"px!important;";
                }
                $this.css("cssText",imgCssText);
			}
			else if(objHeight<objWidth) {
                if(parentHeight/ratio < parentWidth){
                    imgCssText = "width:"+parentWidth+"px!important;height:"+parentWidth*ratio+"px!important;";
                }else{
                    imgCssText = "width:"+parentHeight/ratio+"px!important;height:"+parentHeight+"px!important;";
                }
                $this.css("cssText",imgCssText);
            }else{
                if(parentHeight>parentWidth) {//赋值宽高
                    imgCssText = "width:"+parentHeight+"px!important;height:"+parentHeight+"px!important;";
                    $this.css("cssText",imgCssText);
                }else{
                    imgCssText = "width:"+parentWidth+"px!important;height:"+parentWidth+"px!important;";
                    $this.css("cssText",imgCssText);
                }
			}

/*            objHeight=$this[0].height;//重新获取宽高
            objWidth=$this[0].width;*/
            objHeight = $this.css("height").replace("px","");
            objWidth = $this.css("width").replace("px","");



            if(objHeight>objWidth) {
                if(objHeight == parentHeight) {
                    imgCssText = $this.attr("style")+";margin-left:"+(parentWidth-objWidth)/2+"px!important;border:0px!important;";
                }else{
                    imgCssText = $this.attr("style")+";margin-top:"+(parentHeight-objHeight)/2+"px!important;border:0px!important;";
                }
                $this.css("cssText",imgCssText);
            }else if(objHeight<objWidth) {
                if(objWidth == parentWidth) {
                    imgCssText = $this.attr("style")+";margin-top:"+(parentHeight-objHeight)/2+"px!important;border:0px!important;";
                }else{
                    imgCssText = $this.attr("style")+";margin-left:"+(parentWidth-objWidth)/2+"px!important;border:0px!important;";
                }
                $this.css("cssText",imgCssText);
            }else{
                if(parentHeight >= parentWidth) {
                    imgCssText = $this.attr("style")+";margin-left:"+(parentWidth-objWidth)/2+"px!important;border:0px!important;"
                    $this.css("cssText",imgCssText);
                }else{
                    imgCssText = $this.attr("style")+";margin-top:"+(parentHeight-objHeight)/2+"px!important;border:0px!important;";
                    $this.css("cssText",imgCssText);
                }
            }

        }else{
            try
            {
                var widthrate = objWidth / parentWidth;
                var heightrate = objHeight / parentHeight;
                var imgwidth = 0;
                var imgheight = 0;

                if(widthrate > 1 || heightrate > 1 || widthrate < 1 || heightrate < 1)
                {
                    if(CutImgType == "Scaling")//按比例缩放
                    {
                        var rate = Math.max(widthrate, heightrate);
                        imgwidth = Math.max(1, Math.min(objWidth/rate, parentWidth));
                        imgheight = Math.max(1, Math.min(objHeight/rate, parentHeight));
                    }
                    else//拉伸
                    {
                        imgwidth = parentWidth;
                        imgheight = parentHeight;
                    }
                }
                else //大小合适
                {
                    imgwidth = parentWidth;
                    imgheight = parentHeight;
                }

                imgCssText += "width:"+imgwidth+"px!important;height:"+imgheight+"px!important;";
                $this.css("cssText",imgCssText);

                if(imgwidth>imgheight) {
                    if(imgwidth != parentWidth){
                        imgCssText = $this.attr("style")+";margin-left:"+(parentWidth-imgwidth)/2+"px!important;border:0px!important;";
                    }else{
                        imgCssText = $this.attr("style")+";margin-top:"+(parentHeight-imgheight)/2+"px!important;border:0px!important;";
                    }
                    $this.css("cssText",imgCssText);
                }
                else
                {
                    if(imgheight != parentHeight){
                        imgCssText = $this.attr("style")+";margin-top:"+(parentHeight-imgheight)/2+"px!important;border:0px!important;";
                    }else{
                        imgCssText = $this.attr("style")+";margin-left:"+(parentWidth-imgwidth)/2+"px!important;border:0px!important;";
                    }
                    $this.css("cssText",imgCssText);
                }
            }
            catch(e)
            {
            }
		}
    })
}

