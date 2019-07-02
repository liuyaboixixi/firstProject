/**
 * 提价一级评论
 */
function post() {

var  questionId = $("#question_id").val();
var content = $("#comment_content").val();
comment2target(questionId,1,content);

}
function  comment2target(targetId,type,content) {
    if (!content){
        alert("不能回复空内容....")
        return;
    }
    $.ajax({
        type:"POST",
        url:"/comment",
        contentType: 'application/json',
        data:JSON.stringify({
            "parentId":targetId,
            "content":content,
            "type": type
        }),
        success:function (response) {
            if (response.code==200){
                $("#comment_section").hide();
                window.location.reload();
            }else{
                alert(response.message);
            }
            console.log(response);

        },
        dataType:"json"
    });
}


//回复二级评论
function comment(e){
    var  commentId = e.getAttribute("data-id");
    var content = $("#input-" + commentId).val();
   comment2target(commentId,2,content)
}
/*
* 展开二级评论
* */

function collapseComments(e) {
    var id = e.getAttribute("data-id");
    var comments =$("#comment-" + id);
    //获取二级评论 的展开状态
    var  collapse = e.getAttribute("data-collapse");
    if (collapse){
        //折叠二级评论
        comments.removeClass("in");
        e.removeAttribute("data-collapse");
        e.classList.remove("active")
    } else {
        $.getJSON("/comment/" + id, function (data) {
            var subCommentContainer =$("#comment-"+id);
            console.log(data);
            $.each(data.data.reverse(), function (index, comment) {

                var media = $("<div/>",{})
                var c=$("<div/>",{
                    "class": "col-lg-12 col-md-12 col-sm-12 col-xs-12 comments",
                    html:comment.content
                });
                subCommentContainer.prepend(c);
            });


        //展开二级评论\
        comments.addClass("in");
        //标记二级评论 展开状态
        e.setAttribute("data-collapse","in");
        e.classList.add("active");
        });
    }
    console.log(id);
}